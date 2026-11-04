#!/usr/bin/env python3
"""AI Portal installer (boto3). agentic-work/installer.py 의 구조를 따르되 이 프로젝트에 필요한 것만 만든다.

단계 (모두 idempotent — 다시 실행해도 된다):
  1. AgentCore Registry  `ai-portal` (+ 승인 요청 메일 알림: EventBridge → SNS 이메일)
  2. Secrets Manager     세션 서명 키
  3. IAM                 ECS execution / task role
  4. 네트워크            VPC, public subnet x2, ALB SG, ECS SG
  5. ALB                 CloudFront 에서 오는 요청(secret header)만 통과
  6. ECR + 이미지 빌드   linux/arm64 (Fargate ARM)
  7. ECS Fargate         cluster / task / service (desired=1)
  8. CloudFront          https 진입점 (*.cloudfront.net)
  +  Harness agent       `ai_portal` (MCP 테스트용. AgentCore Harness + 전용 실행 역할)

사용:
  python installer.py --registry-only       # Registry 만 생성
  python installer.py --notifications-only  # 승인 요청 메일 알림(SNS + EventBridge)만 구성
  python installer.py --harness-only        # MCP 테스트용 harness agent `ai_portal` 만 생성
  python installer.py                       # 전체 설치
  python installer.py --skip-docker-build   # 인프라만 갱신(ECR 최신 이미지 재사용)
  python installer.py --image-only          # 이미지 빌드/푸시 후 서비스 재배포
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# ── Configuration ───────────────────────────────────────────────────
project_name = "ai-portal"  # Registry 이름이기도 하다
region = "us-west-2"
VPC_CIDR = "10.23.0.0/16"
CONTAINER_PORT = 8501
ADMIN_EMAILS = "e2npark@gmail.com"

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "application" / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("installer")

session = boto3.Session(region_name=region)
sts = session.client("sts")
account_id = sts.get_caller_identity()["Account"]
ec2 = session.client("ec2")
iam = session.client("iam")
ecr = session.client("ecr")
ecs = session.client("ecs")
elbv2 = session.client("elbv2")
logs = session.client("logs")
secretsmanager = session.client("secretsmanager")
cloudfront = session.client("cloudfront")
events = session.client("events")
agentcore = session.client("bedrock-agentcore-control")

TAGS = [{"Key": "Project", "Value": project_name}]


# ── helpers ─────────────────────────────────────────────────────────
def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(**updates: Any) -> Dict[str, Any]:
    cfg = {**load_config(), "projectName": project_name, "accountId": account_id, "region": region, **updates}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return cfg


def read_env_file(key: str) -> str:
    p = ROOT / ".env"
    if os.environ.get(key):
        return os.environ[key]
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(rf"\s*{key}\s*=\s*(.*)$", line)
            if m:
                return m.group(1).strip().strip('"')
    return ""


def run(cmd: List[str], **kw: Any) -> subprocess.CompletedProcess:
    logger.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


# ── 1. Registry ─────────────────────────────────────────────────────
def find_registry(name: str = project_name) -> Optional[Dict[str, Any]]:
    token = None
    while True:
        r = agentcore.list_registries(**({"nextToken": token} if token else {}))
        for reg in r.get("registries", []):
            if reg["name"] == name:
                return reg
        token = r.get("nextToken")
        if not token:
            return None


def create_registry(auto_approval: bool = False) -> Dict[str, str]:
    """AgentCore Registry `ai-portal` 을 만든다(이미 있으면 재사용). 인증은 IAM, 승인은 수동."""
    logger.info("[1/8] AgentCore Registry '%s'", project_name)
    reg = find_registry()
    if reg:
        logger.warning("Registry already exists: %s (%s)", reg["registryId"], reg["status"])
    else:
        r = agentcore.create_registry(
            name=project_name,
            description="AI Portal — MCP servers, agents, skills and prompts catalog",
            authorizerType="AWS_IAM",
            approvalConfiguration={"autoApproval": auto_approval},
        )
        logger.info("Created: %s", r["registryArn"])
        reg = None
    for _ in range(60):
        reg = find_registry()
        if reg and reg["status"] == "READY":
            break
        if reg and reg["status"].endswith("FAILED"):
            raise RuntimeError(f"Registry {reg['status']}: {reg.get('statusReason', '')}")
        time.sleep(5)
    else:
        raise TimeoutError("Registry did not become READY")
    info = {"registry_id": reg["registryId"], "registry_arn": reg["registryArn"]}
    save_config(**info)
    logger.info("✓ Registry READY: %s", reg["registryId"])
    return info


# ── 1b. 승인 요청 알림 (Basic: EventBridge → SNS 이메일) ─────────────
APPROVAL_DETAIL_TYPE = "Registry Record State changed to Pending Approval"
# 신규 네임스페이스(aws.agent-registry)와 구 네임스페이스(aws.bedrock-agentcore) 둘 다 받는다.
APPROVAL_PATTERN = {"source": ["aws.agent-registry", "aws.bedrock-agentcore"],
                    "detail-type": [APPROVAL_DETAIL_TYPE]}


def portal_origin() -> str:
    """승인 링크의 기준 주소. 배포 후엔 CloudFront 주소, 그 전엔 config.json/ .env 의 값(없으면 로컬)."""
    return (read_env_file("PORTAL_ORIGIN") or load_config().get("sharing_url") or "http://localhost:8501").rstrip("/")


NOTIFY_SENDER_LOCAL = "ai-portal"                       # 발신 주소: ai-portal@<도메인>
NOTIFY_FUNCTION = f"{project_name}-approval-notify"
NOTIFY_ROLE = f"role-lambda-notify-for-{project_name}-{region}"


def _notify_sender() -> str:
    return f"AI Portal <{NOTIFY_SENDER_LOCAL}@{DOMAIN.split('.', 1)[1]}>"      # portal.my-agentic-ai.click → my-agentic-ai.click


def _package_notify_zip() -> bytes:
    import io
    import zipfile
    src = ROOT / "lambda-notify" / "lambda_function.py"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, arcname="lambda_function.py")
    return buf.getvalue()


def _ensure_notify_role() -> str:
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
             "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=NOTIFY_ROLE)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=NOTIFY_ROLE, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS)["Role"]["Arn"]
        time.sleep(10)
    iam.attach_role_policy(RoleName=NOTIFY_ROLE, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=NOTIFY_ROLE, PolicyName="notify-permissions", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "ReadApprovers", "Effect": "Allow", "Action": "ssm:GetParameter",
             "Resource": [f"arn:aws:ssm:{region}:{account_id}:parameter/{project_name}/admins",
                          f"arn:aws:ssm:{region}:{account_id}:parameter/{project_name}/reviewers"]},
            # SES 는 발신 ID 뿐 아니라 수신 주소(identity/<주소>)도 리소스로 검사한다. 수신자는 관리자 목록에서 동적으로
            # 정해지므로 리소스는 이 계정의 SES ID 전체로 열고, 대신 "보낸 사람" 을 조건으로 우리 주소 하나에 고정한다.
            {"Sid": "SendMail", "Effect": "Allow", "Action": "ses:SendEmail",
             "Resource": [f"arn:aws:ses:{region}:{account_id}:identity/*"],
             "Condition": {"StringEquals": {"ses:FromAddress": f"{NOTIFY_SENDER_LOCAL}@{DOMAIN.split('.', 1)[1]}"}}}]}))
    return arn


def ensure_approval_notifications(registry_id: str, origin: Optional[str] = None) -> Dict[str, str]:
    """Registry 레코드가 PENDING_APPROVAL 이 되면 **지정된 승인자 한 명에게만**(지정이 없으면 모든 관리자에게) 메일을 보낸다.

    EventBridge → Lambda(lambda-notify) → SES. SNS 토픽은 구독자 전원에게 가므로 쓰지 않는다.
    메일의 링크 → 포털 로그인(Google) → 해당 record 검토 화면.
    """
    origin = (origin or portal_origin()).rstrip("/")
    lam = session.client("lambda")
    logger.info("[1b] Approval notifications → Lambda %s (SES sender %s, link base %s)", NOTIFY_FUNCTION, _notify_sender(), origin)
    role = _ensure_notify_role()
    cfg = {"Environment": {"Variables": {
        "SENDER": _notify_sender(), "PORTAL_ORIGIN": origin, "ADMIN_EMAILS": ADMIN_EMAILS,
        "ADMINS_PARAM": f"/{project_name}/admins", "REVIEWERS_PARAM": f"/{project_name}/reviewers"}},
        "Timeout": 30, "MemorySize": 128}
    zipped = _package_notify_zip()
    try:
        lam.get_function(FunctionName=NOTIFY_FUNCTION)
        lam.update_function_code(FunctionName=NOTIFY_FUNCTION, ZipFile=zipped)
        lam.get_waiter("function_updated").wait(FunctionName=NOTIFY_FUNCTION)
        lam.update_function_configuration(FunctionName=NOTIFY_FUNCTION, Role=role, **cfg)
    except lam.exceptions.ResourceNotFoundException:
        for attempt in range(6):                 # 새 역할 전파 대기
            try:
                lam.create_function(FunctionName=NOTIFY_FUNCTION, Runtime="python3.12", Role=role, Handler="lambda_function.handler",
                                    Code={"ZipFile": zipped}, Tags={"Project": project_name}, **cfg)
                break
            except ClientError as e:
                if e.response["Error"]["Code"] == "InvalidParameterValueException" and attempt < 5:
                    time.sleep(8)
                    continue
                raise
    lam.get_waiter("function_active_v2").wait(FunctionName=NOTIFY_FUNCTION)
    fn_arn = lam.get_function(FunctionName=NOTIFY_FUNCTION)["Configuration"]["FunctionArn"]

    rule_name = f"{project_name}-approval-requested"
    rule_arn = f"arn:aws:events:{region}:{account_id}:rule/{rule_name}"
    pattern = {**APPROVAL_PATTERN, "detail": {"registryId": [registry_id]}}
    events.put_rule(Name=rule_name, EventPattern=json.dumps(pattern), State="ENABLED", Tags=TAGS,
                    Description=f"{project_name}: record submitted for approval → mail to the assigned approver")
    try:                                          # 이 규칙만 Lambda 를 호출할 수 있게 한다
        lam.add_permission(FunctionName=NOTIFY_FUNCTION, StatementId="events-invoke", Action="lambda:InvokeFunction",
                           Principal="events.amazonaws.com", SourceArn=rule_arn)
    except lam.exceptions.ResourceConflictException:
        pass
    events.put_targets(Rule=rule_name, Targets=[{"Id": "notify", "Arn": fn_arn}])
    try:                                          # 예전 SNS 대상은 제거(이중 발송 방지)
        events.remove_targets(Rule=rule_name, Ids=["email"])
    except ClientError:
        pass
    save_config(approval_lambda=NOTIFY_FUNCTION, approval_rule=rule_name, approval_link_base=origin, approval_sender=_notify_sender())
    return {"lambda": NOTIFY_FUNCTION, "rule": rule_name, "link_base": origin}


# ── 1c. Harness agent (MCP 테스트용) ─────────────────────────────────
HARNESS_MODEL_ID = "us.anthropic.claude-sonnet-5-5"
HARNESS_SYSTEM_PROMPT = (
    "당신은 MCP 서버/Gateway 의 도구를 시험하는 테스트 에이전트입니다. 사용자의 요청을 수행하기 위해 "
    "사용 가능한 도구를 호출하고, 어떤 도구를 어떤 인자로 호출했는지와 결과를 간결하게 알려 주세요. "
    "도구가 없거나 실패하면 그 사실과 오류 내용을 그대로 설명하세요.")


def harness_api_name() -> str:
    """CreateHarness harnessName 은 [a-zA-Z][a-zA-Z0-9_]{0,39} — 하이픈 불가."""
    n = re.sub(r"[^A-Za-z0-9_]", "_", project_name)
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,39}$", n):
        raise ValueError(f"harness name {n!r} is invalid")
    return n


def create_harness_role() -> str:
    """harness 실행 역할. 신뢰: bedrock-agentcore. 권한: 모델 호출 + (MCP 테스트용) Gateway 호출."""
    name = f"role-harness-for-{project_name}-{region}"
    trust = {"Version": "2012-10-17", "Statement": [{"Sid": "AllowAgentCoreAssumeHarness", "Effect": "Allow",
             "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAGS,
                              Description="Execution role for the ai-portal MCP test harness")["Role"]["Arn"]
        logger.info("Created role %s", name)
    iam.put_role_policy(RoleName=name, PolicyName="harness-permissions", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "BedrockModel", "Effect": "Allow", "Action": [
                "bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                "bedrock:GetInferenceProfile", "bedrock:GetFoundationModel"],
             "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{account_id}:inference-profile/*"]},
            # MCP 테스트: harness 가 IAM(SigV4)으로 Gateway 를 호출한다. 이 계정의 Gateway 로 한정.
            {"Sid": "InvokeGateways", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"],
             "Resource": [f"arn:aws:bedrock-agentcore:*:{account_id}:gateway/*"]},
            # harness 는 기본으로 관리형 메모리(memory/<harness>-…)를 만들고 대화 이력을 읽고 쓴다.
            {"Sid": "HarnessMemory", "Effect": "Allow", "Action": [
                "bedrock-agentcore:ListEvents", "bedrock-agentcore:CreateEvent", "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListSessions", "bedrock-agentcore:GetMemory",
                "bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:ListMemoryRecords"],
             "Resource": [f"arn:aws:bedrock-agentcore:{region}:{account_id}:memory/{harness_api_name()}-*"]},
        ]}))
    time.sleep(10)  # IAM 전파
    return arn


def ensure_harness() -> Dict[str, str]:
    """MCP 테스트용 harness agent `ai_portal` 을 만든다(이미 있으면 재사용). 도구는 호출 때마다 지정한다."""
    logger.info("[1c] Harness agent '%s'", harness_api_name())
    api_name = harness_api_name()
    h = next((x for x in agentcore.list_harnesses().get("harnesses", []) if x.get("harnessName") == api_name), None)
    if not h:
        role = create_harness_role()
        for attempt in range(6):
            try:
                h = agentcore.create_harness(
                    harnessName=api_name, executionRoleArn=role,
                    model={"bedrockModelConfig": {"modelId": HARNESS_MODEL_ID, "maxTokens": 4096}},
                    systemPrompt=[{"text": HARNESS_SYSTEM_PROMPT}],
                    maxIterations=10, maxTokens=8192, timeoutSeconds=300,
                    tags={"Project": project_name})["harness"]
                break
            except ClientError as e:
                if e.response["Error"]["Code"] in ("ValidationException",) and "assume" in str(e).lower() and attempt < 5:
                    time.sleep(10)   # 새 역할이 아직 전파되지 않음
                    continue
                raise
        logger.info("Created harness: %s", h["harnessId"])
    for _ in range(60):
        cur = agentcore.get_harness(harnessId=h["harnessId"])["harness"]
        if cur["status"] == "READY":
            break
        if cur["status"].endswith("FAILED"):
            raise RuntimeError(f"Harness {cur['status']}: {cur.get('failureReason', '')}")
        time.sleep(5)
    else:
        raise TimeoutError("Harness did not become READY")
    info = {"harness_id": cur["harnessId"], "harness_arn": cur["arn"], "harness_name": api_name}
    save_config(**info)
    logger.info("✓ Harness READY: %s", cur["arn"])
    return info


# ── 2. Secrets ──────────────────────────────────────────────────────
def ensure_session_secret() -> str:
    name = f"{project_name}/session-signing-key"
    try:
        return secretsmanager.describe_secret(SecretId=name)["ARN"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    r = secretsmanager.create_secret(Name=name, SecretString=secrets.token_urlsafe(48), Tags=TAGS)
    logger.info("Created secret %s", name)
    return r["ARN"]


def ensure_gateway_secret() -> Optional[str]:
    """AWSome AI Gateway 키를 Secrets Manager 에 둔다(없으면 프롬프트 테스트만 비활성)."""
    name = f"{project_name}/llm-gateway-key"
    value = read_env_file("LLM_GATEWAY_KEY")
    try:
        arn = secretsmanager.describe_secret(SecretId=name)["ARN"]
        if value:  # .env 의 값이 있으면 항상 최신으로 맞춘다
            secretsmanager.put_secret_value(SecretId=name, SecretString=value)
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    if not value:
        logger.warning("LLM_GATEWAY_KEY 가 .env 에 없습니다 — 프롬프트 테스트가 비활성화됩니다")
        return None
    logger.info("Created secret %s", name)
    return secretsmanager.create_secret(Name=name, SecretString=value, Tags=TAGS)["ARN"]


# ── 3. IAM ──────────────────────────────────────────────────────────
ECS_TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole",
    "Condition": {"StringEquals": {"aws:SourceAccount": account_id}}}]}


def ensure_role(name: str, managed: Optional[List[str]] = None) -> str:
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(ECS_TRUST), Tags=TAGS)["Role"]["Arn"]
        logger.info("Created role %s", name)
        time.sleep(10)  # IAM propagation
    for p in managed or []:
        iam.attach_role_policy(RoleName=name, PolicyArn=p)
    return arn


def create_ecs_roles(secret_arns: List[str]) -> Dict[str, str]:
    logger.info("[3/8] IAM roles")
    exe = ensure_role(f"role-ecs-execution-for-{project_name}-{region}",
                      ["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"])
    iam.put_role_policy(RoleName=f"role-ecs-execution-for-{project_name}-{region}", PolicyName="read-session-secret",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                            {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns}]}))
    task_name = f"role-ecs-task-for-{project_name}-{region}"
    task = ensure_role(task_name)
    iam.put_role_policy(RoleName=task_name, PolicyName="portal-aws-access", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Sid": "PromptManagement", "Effect": "Allow", "Resource": "*", "Action": [
                "bedrock:CreatePrompt", "bedrock:GetPrompt", "bedrock:UpdatePrompt", "bedrock:DeletePrompt",
                "bedrock:ListPrompts", "bedrock:CreatePromptVersion"]},
            {"Sid": "AgentCoreRead", "Effect": "Allow", "Resource": "*", "Action": [
                "bedrock-agentcore:ListHarnesses", "bedrock-agentcore:GetHarness",
                "bedrock-agentcore:ListAgentRuntimes", "bedrock-agentcore:GetAgentRuntime",
                "bedrock-agentcore:ListRegistries", "bedrock-agentcore:GetRegistry",
                # MCP 탭: AgentCore Gateway 동기화
                "bedrock-agentcore:ListGateways", "bedrock-agentcore:GetGateway"]},
            # Agent/MCP 테스트: 포털이 harness·runtime 을 호출한다
            {"Sid": "InvokeAgents", "Effect": "Allow", "Resource": "*", "Action": [
                "bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime",
                "bedrock-agentcore:InvokeAgentRuntimeForUser",
                # Skill 테스트: harness 세션에 Node.js 설치
                "bedrock-agentcore:InvokeAgentRuntimeCommand"]},
            # 화면에서 추가한 관리자 목록과 record 별 reviewer 는 재배포로 사라지지 않도록 SSM Parameter Store 에 둔다
            {"Sid": "ApproverStore", "Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:PutParameter"],
             "Resource": [f"arn:aws:ssm:{region}:{account_id}:parameter/{project_name}/admins",
                          f"arn:aws:ssm:{region}:{account_id}:parameter/{project_name}/reviewers"]},
            {"Sid": "RegistryRecords", "Effect": "Allow", "Resource": "*", "Action": [
                "bedrock-agentcore:CreateRegistry", "bedrock-agentcore:CreateRegistryRecord",
                "bedrock-agentcore:GetRegistryRecord", "bedrock-agentcore:ListRegistryRecords",
                "bedrock-agentcore:UpdateRegistryRecord", "bedrock-agentcore:SubmitRegistryRecordForApproval",
                # 관리자 승인/거절/deprecate, 삭제·승인 요청 취소
                "bedrock-agentcore:UpdateRegistryRecordStatus", "bedrock-agentcore:DeleteRegistryRecord"]},
        ]}))
    return {"execution_role_arn": exe, "task_role_arn": task}


# ── 4. Network ──────────────────────────────────────────────────────
def _name_filter(name: str) -> List[Dict[str, Any]]:
    return [{"Name": "tag:Name", "Values": [name]}]


def create_vpc() -> Dict[str, Any]:
    logger.info("[4/8] VPC / subnets / security groups")
    name = f"vpc-for-{project_name}"
    vpcs = ec2.describe_vpcs(Filters=_name_filter(name))["Vpcs"]
    if vpcs:
        vpc_id = vpcs[0]["VpcId"]
        logger.warning("VPC exists: %s", vpc_id)
    else:
        used = {v["CidrBlock"] for v in ec2.describe_vpcs()["Vpcs"]}
        if VPC_CIDR in used:
            raise RuntimeError(f"CIDR {VPC_CIDR} is already used by another VPC; change VPC_CIDR")
        vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR, TagSpecifications=[{"ResourceType": "vpc", "Tags": TAGS + [
            {"Key": "Name", "Value": name}]}])["Vpc"]["VpcId"]
        ec2.get_waiter("vpc_available").wait(VpcIds=[vpc_id])
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
        ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})

    igws = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]
    if igws:
        igw = igws[0]["InternetGatewayId"]
    else:
        igw = ec2.create_internet_gateway(TagSpecifications=[{"ResourceType": "internet-gateway", "Tags": TAGS}])[
            "InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc_id)

    azs = [a["ZoneName"] for a in ec2.describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"]][:2]
    subnets: List[str] = []
    for i, az in enumerate(azs):
        sn_name = f"subnet-public-{i + 1}-for-{project_name}"
        found = ec2.describe_subnets(Filters=_name_filter(sn_name) + [{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
        if found:
            subnets.append(found[0]["SubnetId"])
            continue
        sid = ec2.create_subnet(VpcId=vpc_id, AvailabilityZone=az, CidrBlock=f"10.23.{i}.0/24",
                                TagSpecifications=[{"ResourceType": "subnet", "Tags": TAGS + [
                                    {"Key": "Name", "Value": sn_name}]}])["Subnet"]["SubnetId"]
        ec2.modify_subnet_attribute(SubnetId=sid, MapPublicIpOnLaunch={"Value": True})
        subnets.append(sid)

    rt_name = f"rt-public-for-{project_name}"
    rts = ec2.describe_route_tables(Filters=_name_filter(rt_name) + [{"Name": "vpc-id", "Values": [vpc_id]}])["RouteTables"]
    if rts:
        rt = rts[0]["RouteTableId"]
    else:
        rt = ec2.create_route_table(VpcId=vpc_id, TagSpecifications=[{"ResourceType": "route-table", "Tags": TAGS + [
            {"Key": "Name", "Value": rt_name}]}])["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    assoc = {a["SubnetId"] for t in ec2.describe_route_tables(RouteTableIds=[rt])["RouteTables"] for a in t["Associations"]
             if a.get("SubnetId")}
    for sid in subnets:
        if sid not in assoc:
            ec2.associate_route_table(RouteTableId=rt, SubnetId=sid)

    def sg(sg_name: str, desc: str) -> str:      # 주의: 이름이 'sg-' 로 시작하면 AWS 가 거부한다
        f = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [sg_name]},
                                                  {"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
        if f:
            return f[0]["GroupId"]
        return ec2.create_security_group(GroupName=sg_name, Description=desc, VpcId=vpc_id, TagSpecifications=[
            {"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]

    alb_sg = sg(f"alb-sg-for-{project_name}", "ALB: CloudFront origin-facing only")
    ecs_sg = sg(f"ecs-sg-for-{project_name}", "ECS tasks: from ALB only")
    # ALB 는 CloudFront origin-facing managed prefix list 에서 오는 80 만 허용
    pl = ec2.describe_managed_prefix_lists(Filters=[
        {"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}])["PrefixLists"][0]["PrefixListId"]
    for gid, perm in (
        (alb_sg, {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "PrefixListIds": [{"PrefixListId": pl}]}),
        (ecs_sg, {"IpProtocol": "tcp", "FromPort": CONTAINER_PORT, "ToPort": CONTAINER_PORT,
                  "UserIdGroupPairs": [{"GroupId": alb_sg}]}),
    ):
        try:
            ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[perm])
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise
    return {"vpc_id": vpc_id, "subnets": subnets, "alb_sg": alb_sg, "ecs_sg": ecs_sg}


# ── 5. ALB ──────────────────────────────────────────────────────────
def origin_header_value() -> str:
    name = f"{project_name}/alb-origin-header"
    try:
        return secretsmanager.get_secret_value(SecretId=name)["SecretString"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    v = secrets.token_urlsafe(32)
    secretsmanager.create_secret(Name=name, SecretString=v, Tags=TAGS)
    return v


def create_alb(net: Dict[str, Any]) -> Dict[str, str]:
    logger.info("[5/8] ALB")
    alb_name = f"alb-for-{project_name}"
    try:
        alb = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"][0]
    except ClientError as e:
        if e.response["Error"]["Code"] != "LoadBalancerNotFound":
            raise
        alb = elbv2.create_load_balancer(Name=alb_name, Subnets=net["subnets"], SecurityGroups=[net["alb_sg"]],
                                         Scheme="internet-facing", Type="application", Tags=TAGS)["LoadBalancers"][0]
    elbv2.modify_load_balancer_attributes(LoadBalancerArn=alb["LoadBalancerArn"], Attributes=[
        {"Key": "idle_timeout.timeout_seconds", "Value": "120"}])

    tg_name = f"tg-for-{project_name}"[:32]
    try:
        tg = elbv2.describe_target_groups(Names=[tg_name])["TargetGroups"][0]["TargetGroupArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "TargetGroupNotFound":
            raise
        tg = elbv2.create_target_group(
            Name=tg_name, Protocol="HTTP", Port=CONTAINER_PORT, VpcId=net["vpc_id"], TargetType="ip",
            HealthCheckPath="/api/health", HealthCheckIntervalSeconds=30, Tags=TAGS)["TargetGroups"][0]["TargetGroupArn"]

    hdr = origin_header_value()
    listeners = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    if listeners:
        lst = listeners[0]["ListenerArn"]
        rules = elbv2.describe_rules(ListenerArn=lst)["Rules"]
        rule = next((r for r in rules if not r["IsDefault"]), None)
        if rule:
            elbv2.modify_rule(RuleArn=rule["RuleArn"], Conditions=[{"Field": "http-header", "HttpHeaderConfig": {
                "HttpHeaderName": "X-Origin-Verify", "Values": [hdr]}}], Actions=[{"Type": "forward", "TargetGroupArn": tg}])
    else:
        # 기본 동작은 403. CloudFront 가 붙이는 비밀 헤더가 있을 때만 target group 으로 전달.
        lst = elbv2.create_listener(
            LoadBalancerArn=alb["LoadBalancerArn"], Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "fixed-response", "FixedResponseConfig": {
                "StatusCode": "403", "ContentType": "text/plain", "MessageBody": "Forbidden"}}])["Listeners"][0]["ListenerArn"]
        elbv2.create_rule(ListenerArn=lst, Priority=1, Conditions=[{"Field": "http-header", "HttpHeaderConfig": {
            "HttpHeaderName": "X-Origin-Verify", "Values": [hdr]}}], Actions=[{"Type": "forward", "TargetGroupArn": tg}])
    return {"alb_arn": alb["LoadBalancerArn"], "alb_dns": alb["DNSName"], "tg_arn": tg}


# ── 6. ECR + image ──────────────────────────────────────────────────
def ensure_ecr() -> str:
    repo = f"{project_name}"
    try:
        uri = ecr.describe_repositories(repositoryNames=[repo])["repositories"][0]["repositoryUri"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
        uri = ecr.create_repository(repositoryName=repo, imageScanningConfiguration={"scanOnPush": True},
                                    tags=TAGS)["repository"]["repositoryUri"]
    return uri


def build_and_push(repo_uri: str) -> str:
    logger.info("[6/8] Docker build & push (linux/arm64)")
    tag = time.strftime("%Y%m%d-%H%M%S")
    image = f"{repo_uri}:{tag}"
    pw = session.client("ecr").get_authorization_token()["authorizationData"][0]
    import base64
    user, token = base64.b64decode(pw["authorizationToken"]).decode().split(":", 1)
    subprocess.run(["docker", "login", "--username", user, "--password-stdin", pw["proxyEndpoint"]],
                   input=token.encode(), check=True)
    if platform.machine().lower() not in ("arm64", "aarch64"):
        logger.warning("Host is not arm64; buildx will emulate (slow). QEMU must be installed.")
    run(["docker", "buildx", "build", "--platform", "linux/arm64", "-t", image, "--push", str(ROOT)])
    return image


def latest_image(repo_uri: str) -> str:
    imgs = ecr.describe_images(repositoryName=project_name)["imageDetails"]
    tagged = sorted((i for i in imgs if i.get("imageTags")), key=lambda i: i["imagePushedAt"])
    if not tagged:
        raise RuntimeError("ECR 에 이미지가 없습니다. --skip-docker-build 없이 실행하세요.")
    return f"{repo_uri}:{tagged[-1]['imageTags'][0]}"


# ── 7. ECS ──────────────────────────────────────────────────────────
def deploy_ecs(net: Dict[str, Any], alb: Dict[str, str], roles: Dict[str, str], secret_arn: str,
               image: str, public_origin: str, gateway_secret_arn: Optional[str] = None) -> str:
    logger.info("[7/8] ECS Fargate")
    cluster = f"cluster-for-{project_name}"
    ecs.create_cluster(clusterName=cluster, tags=[{"key": "Project", "value": project_name}])
    log_group = f"/ecs/{project_name}"
    try:
        logs.create_log_group(logGroupName=log_group)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    logs.put_retention_policy(logGroupName=log_group, retentionInDays=30)
    try:
        iam.create_service_linked_role(AWSServiceName="ecs.amazonaws.com")
    except ClientError:
        pass

    env = {
        "GOOGLE_CLIENT_ID": read_env_file("GOOGLE_CLIENT_ID"),
        "ADMIN_EMAILS": ADMIN_EMAILS,
        "COOKIE_SECURE": "1",
        "AWS_REGION": region,
        "PORTAL_DB_PATH": "/app/application/data/portal.db",
        "PORTAL_ORIGIN": public_origin,
        "LLM_GATEWAY_URL": read_env_file("LLM_GATEWAY_URL") or "https://llm.my-agentic-ai.click",
        # config.json 은 이미지에 넣지 않는다(.dockerignore) — 앱이 쓰는 값은 환경 변수로 전달한다
        "REGISTRY_ID": load_config().get("registry_id", ""),
        "HARNESS_ARN": load_config().get("harness_arn", ""),
        "GATEWAY_REGIONS": read_env_file("GATEWAY_REGIONS"),
    }
    if not env["GOOGLE_CLIENT_ID"]:
        raise RuntimeError("GOOGLE_CLIENT_ID 가 .env 에 없습니다")
    env = {k: v for k, v in env.items() if v != ""}
    td = ecs.register_task_definition(
        family=f"task-for-{project_name}", networkMode="awsvpc", requiresCompatibilities=["FARGATE"],
        cpu="512", memory="1024", executionRoleArn=roles["execution_role_arn"], taskRoleArn=roles["task_role_arn"],
        runtimePlatform={"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": "app", "image": image, "essential": True,
            "portMappings": [{"containerPort": CONTAINER_PORT}],
            "environment": [{"name": k, "value": v} for k, v in env.items()],
            "secrets": [{"name": "SESSION_SIGNING_KEY", "valueFrom": secret_arn}] + (
                [{"name": "LLM_GATEWAY_KEY", "valueFrom": gateway_secret_arn}] if gateway_secret_arn else []),
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": log_group, "awslogs-region": region, "awslogs-stream-prefix": "app"}},
            "readonlyRootFilesystem": False,
        }], tags=[{"key": "Project", "value": project_name}])["taskDefinition"]["taskDefinitionArn"]

    svc = f"service-for-{project_name}"
    net_cfg = {"awsvpcConfiguration": {"subnets": net["subnets"], "securityGroups": [net["ecs_sg"]],
                                       "assignPublicIp": "ENABLED"}}
    existing = ecs.describe_services(cluster=cluster, services=[svc])["services"]
    if existing and existing[0]["status"] == "ACTIVE":
        ecs.update_service(cluster=cluster, service=svc, taskDefinition=td, networkConfiguration=net_cfg,
                           forceNewDeployment=True)
    else:
        ecs.create_service(
            cluster=cluster, serviceName=svc, taskDefinition=td, desiredCount=1, launchType="FARGATE",
            networkConfiguration=net_cfg, healthCheckGracePeriodSeconds=60,
            deploymentConfiguration={"minimumHealthyPercent": 0, "maximumPercent": 100},  # SQLite 단일 writer
            loadBalancers=[{"targetGroupArn": alb["tg_arn"], "containerName": "app", "containerPort": CONTAINER_PORT}],
            tags=[{"key": "Project", "value": project_name}])
    logger.info("Waiting for service to become stable...")
    ecs.get_waiter("services_stable").wait(cluster=cluster, services=[svc],
                                           WaiterConfig={"Delay": 15, "MaxAttempts": 60})
    return td


# ── 8b. 커스텀 도메인 (Route53 은 별도 계정/프로파일에 있다) ──────────────
DOMAIN = "portal.my-agentic-ai.click"
DNS_PROFILE = "stock"                     # 이 도메인의 Route53 호스팅 영역이 있는 AWS CLI 프로파일
DNS_ZONE_NAME = "my-agentic-ai.click."
ACM_REGION = "us-east-1"                  # CloudFront 용 인증서는 반드시 us-east-1


def _dns_client():
    return boto3.Session(profile_name=DNS_PROFILE).client("route53")


def _dns_zone_id() -> str:
    r = _dns_client()
    zones = [z for z in r.list_hosted_zones()["HostedZones"] if z["Name"] == DNS_ZONE_NAME and not z["Config"]["PrivateZone"]]
    if not zones:
        raise RuntimeError(f"프로파일 {DNS_PROFILE!r} 에 호스팅 영역 {DNS_ZONE_NAME} 이 없습니다")
    return zones[0]["Id"].split("/")[-1]


def ensure_certificate(domain: str = DOMAIN) -> str:
    """CloudFront 용 ACM 인증서(us-east-1)를 만들고 DNS 검증 레코드를 등록해 발급까지 기다린다. 이미 있으면 재사용."""
    acm = session.client("acm", region_name=ACM_REGION)
    arn = None
    found = {}                                  # 같은 도메인의 인증서가 여럿일 수 있다 → 발급된 것을 우선한다
    for page in acm.get_paginator("list_certificates").paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
        for c in page["CertificateSummaryList"]:
            if c["DomainName"] == domain:
                found.setdefault(c["Status"], c["CertificateArn"])
    arn = found.get("ISSUED") or found.get("PENDING_VALIDATION")
    if not arn:
        arn = acm.request_certificate(DomainName=domain, ValidationMethod="DNS",
                                      Tags=[{"Key": "Project", "Value": project_name}])["CertificateArn"]
        logger.info("Requested certificate for %s", domain)
    zone = _dns_zone_id()
    for _ in range(60):
        cert = acm.describe_certificate(CertificateArn=arn)["Certificate"]
        if cert["Status"] == "ISSUED":
            logger.info("✓ Certificate ISSUED: %s", arn)
            return arn
        if cert["Status"] in ("FAILED", "VALIDATION_TIMED_OUT", "REVOKED"):
            raise RuntimeError(f"certificate {cert['Status']}")
        for o in cert.get("DomainValidationOptions", []):
            rr = o.get("ResourceRecord")            # 발급 요청 직후에는 아직 없을 수 있다
            if rr:
                _dns_client().change_resource_record_sets(HostedZoneId=zone, ChangeBatch={
                    "Comment": f"ACM validation for {domain} ({project_name})",
                    "Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
                        "Name": rr["Name"], "Type": rr["Type"], "TTL": 300, "ResourceRecords": [{"Value": rr["Value"]}]}}]})
        time.sleep(10)
    raise TimeoutError("certificate was not issued in time")


def set_distribution_domain(dist_id: str, domain: str, cert_arn: str) -> None:
    """CloudFront 배포에 별칭(CNAME)과 인증서를 붙인다. 이미 같은 설정이면 아무것도 하지 않는다."""
    cur = cloudfront.get_distribution_config(Id=dist_id)
    cfg = cur["DistributionConfig"]
    vc = cfg.get("ViewerCertificate", {})
    if domain in cfg.get("Aliases", {}).get("Items", []) and vc.get("ACMCertificateArn") == cert_arn:
        logger.info("Distribution already serves %s", domain)
        return
    cfg["Aliases"] = {"Quantity": 1, "Items": [domain]}
    cfg["ViewerCertificate"] = {"ACMCertificateArn": cert_arn, "SSLSupportMethod": "sni-only",
                                "MinimumProtocolVersion": "TLSv1.2_2021", "Certificate": cert_arn, "CertificateSource": "acm"}
    cloudfront.update_distribution(Id=dist_id, IfMatch=cur["ETag"], DistributionConfig=cfg)
    logger.info("Attached %s to distribution %s", domain, dist_id)


def point_dns_to_distribution(domain: str, cf_domain: str) -> None:
    """도메인(A/AAAA 별칭)을 CloudFront 로 향하게 한다. CloudFront 별칭의 호스팅 영역 ID 는 고정값(Z2FDTNDATAQYW2)."""
    changes = [{"Action": "UPSERT", "ResourceRecordSet": {
        "Name": domain + ".", "Type": t,
        "AliasTarget": {"HostedZoneId": "Z2FDTNDATAQYW2", "DNSName": cf_domain, "EvaluateTargetHealth": False}}}
        for t in ("A", "AAAA")]
    _dns_client().change_resource_record_sets(HostedZoneId=_dns_zone_id(), ChangeBatch={
        "Comment": f"{project_name}: {domain} → CloudFront", "Changes": changes})
    logger.info("DNS %s → %s", domain, cf_domain)


def setup_domain(dist_id: str, cf_domain: str, domain: str = DOMAIN) -> str:
    cert = ensure_certificate(domain)
    set_distribution_domain(dist_id, domain, cert)
    point_dns_to_distribution(domain, cf_domain)
    save_config(custom_domain=domain, certificate_arn=cert)
    return f"https://{domain}"


# ── 8. CloudFront ───────────────────────────────────────────────────
CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"
ALL_VIEWER_EXCEPT_HOST = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
SECURITY_HEADERS_POLICY = "67f7725c-6f97-4210-82d7-5512b31e9d03"  # managed SecurityHeadersPolicy


DISTRIBUTION_COMMENT = "AI Portal"      # 콘솔에 보이는 CloudFront 배포 설명(이 값으로 기존 배포를 찾는다)


def rename_distribution(dist_id: str, comment: str) -> None:
    """배포 설정을 그대로 두고 Comment 만 바꾼다(UpdateDistribution 은 전체 설정을 요구하므로 읽어서 되돌려 보낸다)."""
    cur = cloudfront.get_distribution_config(Id=dist_id)
    cfg = cur["DistributionConfig"]
    cfg["Comment"] = comment
    cloudfront.update_distribution(Id=dist_id, IfMatch=cur["ETag"], DistributionConfig=cfg)
    logger.info("Renamed distribution %s → %r", dist_id, comment)


def create_cloudfront(alb_dns: str) -> Dict[str, str]:
    logger.info("[8/8] CloudFront")
    comment = DISTRIBUTION_COMMENT
    legacy = f"{project_name} portal"        # 예전 이름: 이미 만든 배포를 인식해 이름만 갱신한다(중복 생성 방지)
    for d in cloudfront.list_distributions().get("DistributionList", {}).get("Items", []) or []:
        if d["Comment"] in (comment, legacy):
            logger.warning("Distribution exists: %s", d["Id"])
            if d["Comment"] != comment:
                rename_distribution(d["Id"], comment)
            return {"distribution_id": d["Id"], "domain": d["DomainName"]}
    methods = {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"],
               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}
    common = {"TargetOriginId": "alb", "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
              "ResponseHeadersPolicyId": SECURITY_HEADERS_POLICY, "OriginRequestPolicyId": ALL_VIEWER_EXCEPT_HOST}
    d = cloudfront.create_distribution(DistributionConfig={
        "CallerReference": f"{project_name}-{int(time.time())}", "Comment": comment, "Enabled": True,
        "HttpVersion": "http2and3", "PriceClass": "PriceClass_200", "DefaultRootObject": "",
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "alb", "DomainName": alb_dns,
            "CustomHeaders": {"Quantity": 1, "Items": [{"HeaderName": "X-Origin-Verify", "HeaderValue": origin_header_value()}]},
            "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
                                   "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 30}}]},
        "DefaultCacheBehavior": {**common, "AllowedMethods": methods, "CachePolicyId": CACHING_DISABLED},
        "CacheBehaviors": {"Quantity": 1, "Items": [{
            **common, "PathPattern": "/assets/*", "CachePolicyId": CACHING_OPTIMIZED,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}}}]},
    })["Distribution"]
    return {"distribution_id": d["Id"], "domain": d["DomainName"]}


# ── main ────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="AI Portal installer")
    ap.add_argument("--registry-only", action="store_true", help="Registry 만 생성하고 종료")
    ap.add_argument("--notifications-only", action="store_true", help="승인 요청 메일 알림만 구성하고 종료")
    ap.add_argument("--harness-only", action="store_true", help="MCP 테스트용 harness agent 만 생성하고 종료")
    ap.add_argument("--domain-only", action="store_true", help="커스텀 도메인(인증서·CloudFront 별칭·DNS)만 설정하고 종료")
    ap.add_argument("--auto-approval", action="store_true", help="Registry auto-approval 켜기(기본: 꺼짐)")
    ap.add_argument("--skip-docker-build", action="store_true", help="ECR 의 최신 이미지를 재사용")
    ap.add_argument("--image-only", action="store_true", help="이미지만 빌드하고 서비스 재배포")
    args = ap.parse_args()

    logger.info("Project=%s Region=%s Account=%s", project_name, region, account_id)
    t0 = time.time()
    if args.harness_only:
        ensure_harness()
        return
    if args.domain_only:
        cfg = load_config()
        if not cfg.get("distribution_id"):
            raise SystemExit("먼저 전체 설치를 실행해 CloudFront 배포를 만드세요")
        url = setup_domain(cfg["distribution_id"], cfg["cloudfront_domain"])
        save_config(sharing_url=url)
        ensure_approval_notifications(create_registry()["registry_id"], url)    # 메일 링크도 새 주소로
        logger.info("✓ %s", url)
        return
    reg = create_registry(args.auto_approval)
    ensure_approval_notifications(reg["registry_id"])
    if args.registry_only or args.notifications_only:
        return

    ensure_harness()
    secret_arn = ensure_session_secret()
    gateway_secret_arn = ensure_gateway_secret()
    roles = create_ecs_roles([secret_arn] + ([gateway_secret_arn] if gateway_secret_arn else []))
    net = create_vpc()
    alb = create_alb(net)
    repo_uri = ensure_ecr()
    image = latest_image(repo_uri) if args.skip_docker_build else build_and_push(repo_uri)
    cf = create_cloudfront(alb["alb_dns"])
    origin = setup_domain(cf["distribution_id"], cf["domain"])        # https://portal.my-agentic-ai.click
    deploy_ecs(net, alb, roles, secret_arn, image, origin, gateway_secret_arn)
    save_config(sharing_url=origin, cloudfront_domain=cf["domain"], distribution_id=cf["distribution_id"],
                alb_dns=alb["alb_dns"], vpc_id=net["vpc_id"], image=image)
    ensure_approval_notifications(reg["registry_id"], origin)   # 메일 링크를 배포 주소로 맞춘다
    logger.info("=" * 60)
    logger.info("✓ Done in %.0fs", time.time() - t0)
    logger.info("URL: %s", origin)
    logger.info("할 일: Google Cloud Console → OAuth client → Authorized JavaScript origins 에 %s 추가", origin)
    logger.info("(CloudFront 배포 전파에 몇 분 걸릴 수 있습니다)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
