#!/usr/bin/env python3
"""HR Advisor MCP installer (boto3).

agentcore-harness/installer.py 의 artifact-share MCP 배포 패턴을 따르며,
테스트용 Aurora PostgreSQL(Serverless v2 + Data API)을 만들고
hr_data_sample.csv 를 적재한 뒤 hr-advisor MCP Runtime을 배포합니다.

단계 (idempotent):
  1. Aurora 네트워크 (DB subnet group + SG) — ai-portal VPC 재사용
  2. Aurora Serverless v2 클러스터 + 인스턴스 (Data API)
  3. employees 테이블 생성 + CSV 적재
  4. IAM role / ECR / Docker 이미지 푸시
  5. AgentCore Runtime (MCP) 생성·갱신
  6. gw-ai-portal-jwt · gw-ai-portal-iam Gateway target 연결

사용:
  python runtime/installer_hr.py
  python runtime/installer_hr.py --db-only
  python runtime/installer_hr.py --mcp-only
  python runtime/installer_hr.py --skip-docker-build
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# ── Configuration ───────────────────────────────────────────────────
project_name = "ai-portal"
region = "us-west-2"
DB_CLUSTER_ID = f"{project_name}-hr"
DB_INSTANCE_ID = f"{project_name}-hr-instance"
DB_NAME = "hrdb"
DB_MASTER_USER = "hradmin"
DB_SUBNET_GROUP = f"{project_name}-hr-subnet"
DB_SG_NAME = f"hr-aurora-for-{project_name}"
RUNTIME_NAME = "hr_advisor"
GATEWAY_TARGET_NAME = "hr-advisor"
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(__file__).resolve().parent
HR_MCP_DIR = RUNTIME_DIR / "hr-advisor"
CSV_PATH = RUNTIME_DIR / "hr_data_sample.csv"
CONFIG_PATH = ROOT / "application" / "config.json"
HR_CONFIG_PATH = HR_MCP_DIR / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("installer_hr")

session = boto3.Session(region_name=region)
sts = session.client("sts")
account_id = sts.get_caller_identity()["Account"]
ec2 = session.client("ec2")
iam = session.client("iam")
ecr = session.client("ecr")
rds = session.client("rds")
rds_data = session.client("rds-data")
agentcore = session.client("bedrock-agentcore-control")

TAGS = [
    {"Key": "Project", "Value": project_name},
    {"Key": "Component", "Value": "hr-advisor"},
]


# ── helpers ─────────────────────────────────────────────────────────
def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(**updates: Any) -> Dict[str, Any]:
    cfg = {
        **load_config(),
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        **updates,
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # MCP 이미지가 읽을 runtime/hr-advisor/config.json 에도 동기화
    hr_keys = {
        k: cfg[k]
        for k in (
            "projectName",
            "accountId",
            "region",
            "hr_aurora_cluster_id",
            "hr_aurora_cluster_arn",
            "hr_aurora_secret_arn",
            "hr_aurora_database",
            "hr_advisor_model_id",
            "hr_advisor_mcp_runtime_arn",
            "hr_advisor_mcp_url",
        )
        if k in cfg
    }
    HR_MCP_DIR.mkdir(parents=True, exist_ok=True)
    HR_CONFIG_PATH.write_text(
        json.dumps(hr_keys, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return cfg


def mcp_runtime_url(agent_runtime_arn: str) -> str:
    encoded = agent_runtime_arn.replace(":", "%3A").replace("/", "%2F")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )


def create_iam_role(
    role_name: str,
    assume_role_policy: Dict[str, Any],
    description: str,
) -> str:
    try:
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=description,
            Tags=TAGS,
        )
        logger.info("✓ IAM role created: %s", role_name)
        return response["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        logger.warning("IAM role already exists: %s", role_name)
        iam.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(assume_role_policy),
        )
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]


def attach_inline_policy(
    role_name: str, policy_name: str, policy_document: Dict[str, Any]
) -> None:
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name[:128],
        PolicyDocument=json.dumps(policy_document),
    )


def _ensure_ecr_repository(repository_name: str) -> None:
    try:
        ecr.describe_repositories(repositoryNames=[repository_name])
        logger.info("  ECR repository exists: %s", repository_name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        logger.info("  Creating ECR repository: %s", repository_name)
        ecr.create_repository(
            repositoryName=repository_name,
            tags=[{"Key": t["Key"], "Value": t["Value"]} for t in TAGS],
        )


def _docker_ecr_login() -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
    username, password = base64.b64decode(token).decode("utf-8").split(":")
    registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"
    process = subprocess.Popen(
        ["docker", "login", "--username", username, "--password-stdin", registry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = process.communicate(input=password)
    if process.returncode != 0:
        raise RuntimeError(f"Docker ECR login failed: {stderr}")


def _run_docker(cmd: List[str], description: str) -> None:
    logger.info("  %s: %s", description, " ".join(cmd))
    subprocess.run(cmd, check=True)


def _find_agent_runtime_by_name(runtime_name: str) -> Optional[Dict[str, Any]]:
    next_token = None
    while True:
        kwargs: Dict[str, Any] = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore.list_agent_runtimes(**kwargs)
        for item in response.get("agentRuntimes", []):
            if item.get("agentRuntimeName") == runtime_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


# ── 1. Aurora networking ────────────────────────────────────────────
def _portal_vpc_and_subnets(cfg: Dict[str, Any]) -> Tuple[str, List[str]]:
    vpc_id = cfg.get("vpc_id")
    if not vpc_id:
        raise RuntimeError(
            "application/config.json 에 vpc_id 가 없습니다. "
            "먼저 `python installer.py` 로 ai-portal 네트워크를 구성하세요."
        )
    subnets = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )["Subnets"]
    # AZ당 하나씩 (DB subnet group은 2+ AZ 필요)
    by_az: Dict[str, str] = {}
    for sn in sorted(subnets, key=lambda s: s["SubnetId"]):
        by_az.setdefault(sn["AvailabilityZone"], sn["SubnetId"])
    subnet_ids = list(by_az.values())
    if len(subnet_ids) < 2:
        raise RuntimeError(
            f"VPC {vpc_id} 에 서로 다른 AZ 서브넷이 2개 이상 필요합니다 "
            f"(found {len(subnet_ids)})"
        )
    return vpc_id, subnet_ids[:2]


def ensure_db_network(cfg: Dict[str, Any]) -> Dict[str, str]:
    logger.info("[1/6] Aurora DB subnet group / security group")
    vpc_id, subnet_ids = _portal_vpc_and_subnets(cfg)

    try:
        rds.describe_db_subnet_groups(DBSubnetGroupName=DB_SUBNET_GROUP)
        logger.warning("DB subnet group exists: %s", DB_SUBNET_GROUP)
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBSubnetGroupNotFoundFault":
            raise
        rds.create_db_subnet_group(
            DBSubnetGroupName=DB_SUBNET_GROUP,
            DBSubnetGroupDescription=f"HR Advisor Aurora subnets for {project_name}",
            SubnetIds=subnet_ids,
            Tags=TAGS,
        )
        logger.info("✓ Created DB subnet group: %s", DB_SUBNET_GROUP)

    existing_sg = ec2.describe_security_groups(
        Filters=[
            {"Name": "group-name", "Values": [DB_SG_NAME]},
            {"Name": "vpc-id", "Values": [vpc_id]},
        ]
    )["SecurityGroups"]
    if existing_sg:
        sg_id = existing_sg[0]["GroupId"]
        logger.warning("Security group exists: %s (%s)", DB_SG_NAME, sg_id)
    else:
        sg_id = ec2.create_security_group(
            GroupName=DB_SG_NAME,
            Description="HR Advisor Aurora (Data API; no inbound required)",
            VpcId=vpc_id,
            TagSpecifications=[
                {"ResourceType": "security-group", "Tags": TAGS + [{"Key": "Name", "Value": DB_SG_NAME}]}
            ],
        )["GroupId"]
        logger.info("✓ Created security group: %s", sg_id)

    info = {
        "hr_aurora_vpc_id": vpc_id,
        "hr_aurora_subnet_group": DB_SUBNET_GROUP,
        "hr_aurora_sg_id": sg_id,
    }
    save_config(**info)
    return info


# ── 2. Aurora cluster ───────────────────────────────────────────────
def _wait_cluster_available(cluster_id: str, timeout: int = 1800) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
        status = cluster.get("Status", "")
        logger.info("  Aurora cluster status: %s", status)
        if status == "available":
            return cluster
        if status in ("deleting", "incompatible-network", "migration-failed"):
            raise RuntimeError(f"Aurora cluster terminal status: {status}")
        time.sleep(20)
    raise TimeoutError(f"Timed out waiting for Aurora cluster {cluster_id}")


def _wait_instance_available(instance_id: str, timeout: int = 1800) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        inst = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"][0]
        status = inst.get("DBInstanceStatus", "")
        logger.info("  Aurora instance status: %s", status)
        if status == "available":
            return
        if status in ("deleting", "failed", "incompatible-network"):
            raise RuntimeError(f"Aurora instance terminal status: {status}")
        time.sleep(20)
    raise TimeoutError(f"Timed out waiting for Aurora instance {instance_id}")


def ensure_aurora_cluster(net: Dict[str, str]) -> Dict[str, str]:
    logger.info("[2/6] Aurora Serverless v2 cluster (Data API)")
    try:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=DB_CLUSTER_ID)["DBClusters"][0]
        logger.warning("Aurora cluster exists: %s", DB_CLUSTER_ID)
    except ClientError as e:
        if e.response["Error"]["Code"] != "DBClusterNotFoundFault":
            raise
        logger.info("  Creating Aurora cluster: %s", DB_CLUSTER_ID)
        rds.create_db_cluster(
            DBClusterIdentifier=DB_CLUSTER_ID,
            Engine="aurora-postgresql",
            EngineVersion="16.8",
            DatabaseName=DB_NAME,
            MasterUsername=DB_MASTER_USER,
            ManageMasterUserPassword=True,
            DBSubnetGroupName=net["hr_aurora_subnet_group"],
            VpcSecurityGroupIds=[net["hr_aurora_sg_id"]],
            # 테스트용 최소 사양: 유휴 시 0 ACU로 일시정지, 최대 1 ACU
            ServerlessV2ScalingConfiguration={
                "MinCapacity": 0.0,
                "MaxCapacity": 1.0,
                "SecondsUntilAutoPause": 1800,  # 30분
            },
            StorageEncrypted=True,
            EnableHttpEndpoint=True,
            DeletionProtection=False,
            BackupRetentionPeriod=1,
            Tags=TAGS,
        )
        cluster = _wait_cluster_available(DB_CLUSTER_ID)

    cluster = _wait_cluster_available(DB_CLUSTER_ID)
    cluster_arn = cluster["DBClusterArn"]
    secret_arn = (cluster.get("MasterUserSecret") or {}).get("SecretArn") or ""
    if not secret_arn:
        # 간헐적으로 describe 직후 SecretArn 이 비어 있을 수 있음
        time.sleep(5)
        cluster = rds.describe_db_clusters(DBClusterIdentifier=DB_CLUSTER_ID)["DBClusters"][0]
        secret_arn = (cluster.get("MasterUserSecret") or {}).get("SecretArn") or ""
    if not secret_arn:
        raise RuntimeError("Aurora MasterUserSecret.SecretArn 을 찾지 못했습니다")

    if not cluster.get("HttpEndpointEnabled"):
        logger.info("  Enabling Data API (HTTP endpoint)")
        try:
            rds.enable_http_endpoint(ResourceArn=cluster_arn)
        except ClientError as e:
            # 일부 엔진/리전은 create 시 EnableHttpEndpoint 만으로 충분
            logger.warning("  enable_http_endpoint: %s", e)

    try:
        rds.describe_db_instances(DBInstanceIdentifier=DB_INSTANCE_ID)
        logger.warning("Aurora instance exists: %s", DB_INSTANCE_ID)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code not in ("DBInstanceNotFound", "DBInstanceNotFoundFault"):
            raise
        logger.info("  Creating Aurora Serverless v2 instance: %s", DB_INSTANCE_ID)
        rds.create_db_instance(
            DBInstanceIdentifier=DB_INSTANCE_ID,
            DBInstanceClass="db.serverless",
            Engine="aurora-postgresql",
            DBClusterIdentifier=DB_CLUSTER_ID,
            PubliclyAccessible=False,
            Tags=TAGS,
        )
    _wait_instance_available(DB_INSTANCE_ID)

    info = {
        "hr_aurora_cluster_id": DB_CLUSTER_ID,
        "hr_aurora_cluster_arn": cluster_arn,
        "hr_aurora_secret_arn": secret_arn,
        "hr_aurora_database": DB_NAME,
        "hr_aurora_instance_id": DB_INSTANCE_ID,
        "hr_advisor_model_id": DEFAULT_MODEL_ID,
    }
    save_config(**info)
    logger.info("✓ Aurora ready: %s", cluster_arn)
    return info


# ── 3. Schema + CSV load ────────────────────────────────────────────
def _exec_sql(cluster_arn: str, secret_arn: str, sql: str) -> Dict[str, Any]:
    return rds_data.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=DB_NAME,
        sql=sql.rstrip(";"),
    )


def _batch_insert(
    cluster_arn: str,
    secret_arn: str,
    sql: str,
    parameter_sets: List[List[Dict[str, Any]]],
    chunk: int = 50,
) -> None:
    for i in range(0, len(parameter_sets), chunk):
        part = parameter_sets[i : i + chunk]
        rds_data.batch_execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=DB_NAME,
            sql=sql,
            parameterSets=part,
        )
        logger.info("  Inserted rows %s–%s", i + 1, i + len(part))


def load_hr_sample_data(db: Dict[str, str]) -> None:
    logger.info("[3/6] Creating employees table and loading %s", CSV_PATH.name)
    if not CSV_PATH.is_file():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    cluster_arn = db["hr_aurora_cluster_arn"]
    secret_arn = db["hr_aurora_secret_arn"]

    # Data API가 준비될 때까지 짧게 재시도
    ddl = """
    CREATE TABLE IF NOT EXISTS employees (
        emp_id      VARCHAR(32) PRIMARY KEY,
        name        VARCHAR(100) NOT NULL,
        gender      VARCHAR(10),
        birth_date  DATE,
        address     VARCHAR(255),
        department  VARCHAR(100),
        job_title   VARCHAR(100),
        job_grade   VARCHAR(50),
        hire_date   DATE,
        email       VARCHAR(255),
        phone       VARCHAR(50)
    )
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, 13):
        try:
            _exec_sql(cluster_arn, secret_arn, ddl)
            last_err = None
            break
        except ClientError as e:
            last_err = e
            logger.warning("  Data API not ready (attempt %s/12): %s", attempt, e)
            time.sleep(15)
    if last_err:
        raise last_err

    count_resp = _exec_sql(cluster_arn, secret_arn, "SELECT COUNT(*) FROM employees")
    existing = int(
        (count_resp.get("records") or [[{"longValue": 0}]])[0][0].get("longValue", 0)
    )
    if existing > 0:
        logger.warning("employees 테이블에 이미 %s행이 있어 CSV 적재를 건너뜁니다", existing)
        return

    insert_sql = (
        "INSERT INTO employees "
        "(emp_id, name, gender, birth_date, address, department, "
        "job_title, job_grade, hire_date, email, phone) "
        "VALUES (:emp_id, :name, :gender, :birth_date, :address, :department, "
        ":job_title, :job_grade, :hire_date, :email, :phone)"
    )
    parameter_sets: List[List[Dict[str, Any]]] = []
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            parameter_sets.append(
                [
                    {"name": "emp_id", "value": {"stringValue": row["emp_id"].strip()}},
                    {"name": "name", "value": {"stringValue": row["name"].strip()}},
                    {"name": "gender", "value": {"stringValue": row["gender"].strip()}},
                    {
                        "name": "birth_date",
                        "value": {"stringValue": row["birth_date"].strip()},
                        "typeHint": "DATE",
                    },
                    {"name": "address", "value": {"stringValue": row["address"].strip()}},
                    {
                        "name": "department",
                        "value": {"stringValue": row["department"].strip()},
                    },
                    {
                        "name": "job_title",
                        "value": {"stringValue": row["job_title"].strip()},
                    },
                    {
                        "name": "job_grade",
                        "value": {"stringValue": row["job_grade"].strip()},
                    },
                    {
                        "name": "hire_date",
                        "value": {"stringValue": row["hire_date"].strip()},
                        "typeHint": "DATE",
                    },
                    {"name": "email", "value": {"stringValue": row["email"].strip()}},
                    {"name": "phone", "value": {"stringValue": row["phone"].strip()}},
                ]
            )

    _batch_insert(cluster_arn, secret_arn, insert_sql, parameter_sets)
    verify = _exec_sql(cluster_arn, secret_arn, "SELECT COUNT(*) FROM employees")
    total = int((verify.get("records") or [[{"longValue": 0}]])[0][0].get("longValue", 0))
    logger.info("✓ Loaded %s employee rows", total)
    save_config(hr_aurora_row_count=total)


# ── 4–5. MCP Runtime ────────────────────────────────────────────────
def create_hr_advisor_mcp_role(db: Dict[str, str]) -> str:
    logger.info("[4/6] Creating HR Advisor MCP Runtime IAM role")
    role_name = f"role-hr-advisor-mcp-for-{project_name}-{region}"
    if len(role_name) > 64:
        role_name = f"role-hr-mcp-{project_name[:20]}-{region}"[:64]

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            },
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": "sts:AssumeRole",
            },
        ],
    }
    role_arn = create_iam_role(
        role_name,
        assume_role_policy,
        description="Execution role for HR Advisor MCP AgentCore Runtime",
    )

    cluster_arn = db["hr_aurora_cluster_arn"]
    secret_arn = db["hr_aurora_secret_arn"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RdsDataApi",
                "Effect": "Allow",
                "Action": [
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                "Resource": [cluster_arn],
            },
            {
                "Sid": "SecretsManagerGet",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [secret_arn],
            },
            {
                "Sid": "BedrockConverse",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "EcrPull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:DescribeImages",
                    "ecr:DescribeRepositories",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{region}:{account_id}:log-group:/aws/bedrock-agentcore/*:log-stream:*",
                ],
            },
            {
                "Sid": "CloudWatchMetrics",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                "Resource": ["*"],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"hr-advisor-mcp-inline-for-{project_name}"[:128],
        policy,
    )
    logger.info("✓ HR Advisor MCP Runtime role ready: %s", role_arn)
    return role_arn


def push_hr_advisor_mcp_image() -> Tuple[str, str]:
    logger.info("[5/6] Building HR Advisor MCP Docker image and pushing to ECR")
    if not shutil.which("docker"):
        raise RuntimeError("docker is required to build the HR Advisor MCP image")
    if not HR_MCP_DIR.is_dir():
        raise RuntimeError(f"MCP directory not found: {HR_MCP_DIR}")
    try:
        sts.get_caller_identity()
    except NoCredentialsError as e:
        raise RuntimeError("AWS credentials are not configured") from e

    repository = RUNTIME_NAME
    image_tag = datetime.now().strftime("%Y%m%d%H%M%S")
    local_tag = f"{repository}:{image_tag}"
    ecr_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"

    _ensure_ecr_repository(repository)
    _docker_ecr_login()
    _run_docker(
        [
            "docker",
            "build",
            "--platform",
            "linux/arm64",
            "--provenance=false",
            "--sbom=false",
            "-t",
            local_tag,
            str(HR_MCP_DIR),
        ],
        "Building Docker image",
    )
    _run_docker(["docker", "tag", local_tag, ecr_uri], "Tagging for ECR")
    _run_docker(["docker", "push", ecr_uri], "Pushing to ECR")
    logger.info("✓ Pushed HR Advisor MCP image: %s", ecr_uri)
    return repository, image_tag


def _wait_agent_runtime_ready(runtime_id: str, timeout: int = 900) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime = agentcore.get_agent_runtime(agentRuntimeId=runtime_id)
        status = runtime.get("status", "")
        logger.info("  Agent runtime status: %s", status)
        if status == "READY":
            return runtime
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            raise RuntimeError(
                f"Agent runtime {runtime_id} terminal status: {status} "
                f"reasons={runtime.get('statusReasons')}"
            )
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for agent runtime {runtime_id}")


def create_or_update_hr_advisor_mcp_runtime(
    role_arn: str,
    repository: str,
    image_tag: str,
    db: Dict[str, str],
) -> Dict[str, str]:
    logger.info("[5.5/6] Creating/updating HR Advisor MCP AgentCore Runtime")
    container_uri = (
        f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repository}:{image_tag}"
    )
    env_vars = {
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
        "PROJECT_NAME": project_name,
        "AURORA_CLUSTER_ARN": db["hr_aurora_cluster_arn"],
        "AURORA_SECRET_ARN": db["hr_aurora_secret_arn"],
        "AURORA_DATABASE": db.get("hr_aurora_database") or DB_NAME,
        "BEDROCK_MODEL_ID": db.get("hr_advisor_model_id") or DEFAULT_MODEL_ID,
    }

    existing = _find_agent_runtime_by_name(RUNTIME_NAME)
    if existing:
        runtime_id = existing["agentRuntimeId"]
        logger.info("  Updating existing runtime: %s (%s)", RUNTIME_NAME, runtime_id)
        response = agentcore.update_agent_runtime(
            agentRuntimeId=runtime_id,
            description="AI Portal HR Advisor MCP (Aurora text-to-SQL)",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            roleArn=role_arn,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        agent_runtime_arn = response["agentRuntimeArn"]
        runtime_id = response.get("agentRuntimeId") or runtime_id
    else:
        logger.info("  Creating runtime: %s", RUNTIME_NAME)
        response = agentcore.create_agent_runtime(
            agentRuntimeName=RUNTIME_NAME,
            description="AI Portal HR Advisor MCP (Aurora text-to-SQL)",
            agentRuntimeArtifact={
                "containerConfiguration": {"containerUri": container_uri}
            },
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            protocolConfiguration={"serverProtocol": "MCP"},
            environmentVariables=env_vars,
            platformVersion="V2",
        )
        agent_runtime_arn = response["agentRuntimeArn"]
        runtime_id = response["agentRuntimeId"]

    _wait_agent_runtime_ready(runtime_id)
    # IAM / resource-policy 전파 여유
    time.sleep(15)
    mcp_url = mcp_runtime_url(agent_runtime_arn)
    info = {
        "agent_runtime_arn": agent_runtime_arn,
        "hr_advisor_mcp_runtime_arn": agent_runtime_arn,
        "hr_advisor_mcp_url": mcp_url,
        "ecr_repository": repository,
        "latest_image_tag": image_tag,
        "agent_runtime_role": role_arn,
        "hr_advisor_mcp_role": role_arn,
        "hr_advisor_mcp_ecr_repository": repository,
        "hr_advisor_mcp_image_tag": image_tag,
    }
    save_config(**{k: v for k, v in info.items() if k.startswith("hr_")})
    logger.info("✓ HR Advisor MCP Runtime: %s", agent_runtime_arn)
    logger.info("  MCP URL: %s", mcp_url)
    return info


# ── 6. Gateway target ───────────────────────────────────────────────
def _gateway_role_arn(cfg: Dict[str, Any]) -> str:
    role_name = f"role-gateway-for-{project_name}-{region}"
    try:
        return iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except ClientError:
        # config 에 없을 수도 있어 이름 규칙으로 조회
        pass
    raise RuntimeError(
        f"Gateway role {role_name} 을 찾지 못했습니다. "
        "`python installer.py --gateway-only` 를 먼저 실행하세요."
    )


def _ensure_gateway_can_invoke_runtime(gateway_role_arn: str) -> None:
    role_name = gateway_role_arn.rsplit("/", 1)[-1]
    policy_name = "hr-advisor-invoke-runtime"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeHrAdvisorRuntime",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/*",
                ],
            }
        ],
    }
    attach_inline_policy(role_name, policy_name, policy)
    logger.info("  ✓ Gateway role can InvokeAgentRuntime")


def put_mcp_runtime_resource_policy(
    agent_runtime_arn: str,
    gateway_role_arn: str,
) -> None:
    harness_role_arn = (
        f"arn:aws:iam::{account_id}:role/role-harness-for-{project_name}-{region}"
    )
    principals = [gateway_role_arn]
    try:
        iam.get_role(RoleName=harness_role_arn.rsplit("/", 1)[-1])
        principals.append(harness_role_arn)
    except ClientError:
        pass

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowInvokeFromGatewayAndHarness",
                "Effect": "Allow",
                "Principal": {
                    "AWS": principals if len(principals) > 1 else principals[0]
                },
                "Action": [
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                "Resource": agent_runtime_arn,
            }
        ],
    }
    try:
        agentcore.put_resource_policy(
            resourceArn=agent_runtime_arn,
            policy=json.dumps(policy),
        )
        logger.info("  ✓ Resource policy set on MCP Runtime")
    except ClientError as e:
        logger.warning("  Could not put resource policy on MCP Runtime: %s", e)


def _find_gateway_target(gateway_id: str, target_name: str) -> Optional[Dict[str, Any]]:
    for target in agentcore.list_gateway_targets(gatewayIdentifier=gateway_id).get(
        "items"
    ) or []:
        if target.get("name") == target_name:
            return target
    return None


def _wait_gateway_target_ready(gateway_id: str, target_id: str, timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = agentcore.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        status = target.get("status", "")
        if status == "READY":
            logger.info("  Gateway target ready: %s", target_id)
            return
        if status.endswith("FAILED") or status.endswith("UNSUCCESSFUL"):
            raise RuntimeError(
                f"Gateway target {target_id} terminal status: {status} "
                f"reasons={target.get('statusReasons')}"
            )
        logger.info("  Waiting for gateway target %s: %s", target_id, status)
        time.sleep(8)
    raise TimeoutError(f"Timed out waiting for gateway target {target_id}")


def _attach_hr_target(gateway_id: str, mcp_url: str) -> str:
    """gateway 하나에 hr-advisor target 을 만들거나(없으면) 갱신하고 READY 가 될 때까지 기다린다. target ID 를 돌려준다."""
    existing = _find_gateway_target(gateway_id, GATEWAY_TARGET_NAME)
    target_id = existing.get("targetId") if existing else None

    if existing and existing.get("status") == "FAILED" and target_id:
        logger.warning("  Gateway target FAILED; deleting before recreate")
        agentcore.delete_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )
        for _ in range(60):
            try:
                agentcore.get_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=target_id
                )
                time.sleep(5)
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    break
                raise
        target_id = None

    target_configuration = {"mcp": {"mcpServer": {"endpoint": mcp_url}}}
    # Runtime MCP target 은 GATEWAY_IAM_ROLE + iamCredentialProvider 가 필수
    credential_provider_configurations = [
        {
            "credentialProviderType": "GATEWAY_IAM_ROLE",
            "credentialProvider": {
                "iamCredentialProvider": {
                    "service": "bedrock-agentcore",
                    "region": region,
                }
            },
        }
    ]

    last_error: Optional[Exception] = None
    for attempt in range(1, 4):
        existing = _find_gateway_target(gateway_id, GATEWAY_TARGET_NAME)
        target_id = existing.get("targetId") if existing else None
        if existing and existing.get("status") == "FAILED" and target_id:
            logger.warning(
                "  Gateway target FAILED (attempt %s); deleting before recreate",
                attempt,
            )
            agentcore.delete_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            for _ in range(60):
                try:
                    agentcore.get_gateway_target(
                        gatewayIdentifier=gateway_id, targetId=target_id
                    )
                    time.sleep(5)
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ResourceNotFoundException":
                        break
                    raise
            target_id = None
            time.sleep(10)

        try:
            if not target_id:
                logger.info(
                    "  Creating gateway target '%s' → %s", GATEWAY_TARGET_NAME, mcp_url
                )
                created = agentcore.create_gateway_target(
                    gatewayIdentifier=gateway_id,
                    name=GATEWAY_TARGET_NAME,
                    description="AgentCore Runtime MCP (HR Advisor / Aurora text-to-SQL)",
                    targetConfiguration=target_configuration,
                    credentialProviderConfigurations=credential_provider_configurations,
                )
                target_id = created["targetId"]
            else:
                logger.info("  Updating gateway target %s", target_id)
                agentcore.update_gateway_target(
                    gatewayIdentifier=gateway_id,
                    targetId=target_id,
                    name=GATEWAY_TARGET_NAME,
                    description="AgentCore Runtime MCP (HR Advisor / Aurora text-to-SQL)",
                    targetConfiguration=target_configuration,
                    credentialProviderConfigurations=credential_provider_configurations,
                )

            _wait_gateway_target_ready(gateway_id, target_id)
            last_error = None
            break
        except Exception as e:
            last_error = e
            logger.warning("  Gateway target attempt %s failed: %s", attempt, e)
            time.sleep(20)

    if last_error or not target_id:
        raise RuntimeError(f"Failed to attach gateway target: {last_error}")

    try:
        agentcore.synchronize_gateway_targets(
            gatewayIdentifier=gateway_id, targetIdList=[target_id]
        )
        _wait_gateway_target_ready(gateway_id, target_id)
    except ClientError as e:
        logger.warning("  synchronize_gateway_targets: %s", e)
    return target_id


# config.json 의 gateway 키 접두사 → hr-advisor target ID 를 기록할 키 (포털 리전의 JWT·IAM gateway 두 개에 같은 target 을 붙인다)
HR_TARGET_KEYS = {"gateway_id": "hr_advisor_mcp_gateway_target_id", "gateway_iam_id": "hr_advisor_mcp_gateway_iam_target_id"}


def ensure_hr_advisor_gateway_target(mcp_info: Dict[str, str]) -> Dict[str, str]:
    logger.info("[6/6] Attaching HR Advisor MCP to project Gateways (jwt · iam)")
    cfg = load_config()
    gateways = [(k, cfg[k]) for k in HR_TARGET_KEYS if cfg.get(k)]
    if not gateways:
        raise RuntimeError(
            "config.json 에 gateway_id 가 없습니다. "
            "`python installer.py --gateway-only` 를 먼저 실행하세요."
        )

    gateway_role_arn = _gateway_role_arn(cfg)
    _ensure_gateway_can_invoke_runtime(gateway_role_arn)
    put_mcp_runtime_resource_policy(
        agent_runtime_arn=mcp_info["agent_runtime_arn"],
        gateway_role_arn=gateway_role_arn,
    )

    info: Dict[str, str] = {}
    for key, gateway_id in gateways:
        logger.info("  gateway %s (%s)", gateway_id, key)
        info[HR_TARGET_KEYS[key]] = _attach_hr_target(gateway_id, mcp_info["hr_advisor_mcp_url"])
    save_config(**info)
    logger.info("✓ Gateway targets ready: %s", info)
    return info


# ── main ────────────────────────────────────────────────────────────
def deploy_db() -> Dict[str, str]:
    cfg = load_config()
    net = ensure_db_network(cfg)
    db = ensure_aurora_cluster(net)
    load_hr_sample_data(db)
    return db


def ensure_all(*, skip_docker: bool = False) -> Dict[str, str]:
    """Aurora + HR Advisor MCP Runtime + gw-ai-portal-jwt/iam target (idempotent).

    `ai-portal/installer.py` 가 호출하는 진입점.
    """
    db_info = deploy_db()
    return deploy_mcp(db_info, skip_docker=skip_docker)


def deploy_mcp(db: Optional[Dict[str, str]] = None, skip_docker: bool = False) -> Dict[str, str]:
    cfg = load_config()
    db_info = dict(db or {})
    for key in (
        "hr_aurora_cluster_arn",
        "hr_aurora_secret_arn",
        "hr_aurora_database",
        "hr_advisor_model_id",
    ):
        if not db_info.get(key) and cfg.get(key):
            db_info[key] = cfg[key]
    for required in ("hr_aurora_cluster_arn", "hr_aurora_secret_arn"):
        if not db_info.get(required):
            raise RuntimeError(
                f"{required} 가 없습니다. --db-only 없이 전체 설치하거나 "
                "먼저 DB 단계를 완료하세요."
            )
    db_info.setdefault("hr_aurora_database", DB_NAME)
    db_info.setdefault("hr_advisor_model_id", DEFAULT_MODEL_ID)

    # Docker 빌드 전에 config.json 을 hr-advisor 에 기록
    save_config(**{k: v for k, v in db_info.items() if k.startswith("hr_")})

    role_arn = create_hr_advisor_mcp_role(db_info)
    if skip_docker:
        repository = cfg.get("hr_advisor_mcp_ecr_repository") or RUNTIME_NAME
        image_tag = cfg.get("hr_advisor_mcp_image_tag")
        if not image_tag:
            raise RuntimeError(
                "--skip-docker-build 에는 기존 hr_advisor_mcp_image_tag 가 필요합니다"
            )
        logger.warning("Skipping docker build; reusing image tag %s", image_tag)
    else:
        repository, image_tag = push_hr_advisor_mcp_image()

    mcp_info = create_or_update_hr_advisor_mcp_runtime(
        role_arn=role_arn,
        repository=repository,
        image_tag=image_tag,
        db=db_info,
    )
    target_info = ensure_hr_advisor_gateway_target(mcp_info)
    mcp_info.update(target_info)
    return mcp_info


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy HR Advisor Aurora + MCP Runtime")
    parser.add_argument("--db-only", action="store_true", help="Aurora + CSV only")
    parser.add_argument("--mcp-only", action="store_true", help="MCP Runtime only")
    parser.add_argument(
        "--skip-docker-build",
        action="store_true",
        help="Reuse last pushed ECR image tag from config.json",
    )
    args = parser.parse_args()

    logger.info("HR Advisor installer (account=%s region=%s)", account_id, region)
    db_info: Optional[Dict[str, str]] = None
    if not args.mcp_only:
        db_info = deploy_db()
    if not args.db_only:
        mcp_info = deploy_mcp(db_info, skip_docker=args.skip_docker_build)
        logger.info("Done. runtime_arn=%s", mcp_info.get("hr_advisor_mcp_runtime_arn"))
        logger.info("MCP URL: %s", mcp_info.get("hr_advisor_mcp_url"))
    else:
        logger.info("Done (DB only). cluster=%s", (db_info or {}).get("hr_aurora_cluster_id"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logger.exception("installer_hr failed: %s", exc)
        raise SystemExit(1)
