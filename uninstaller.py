#!/usr/bin/env python3
"""AI Portal uninstaller (boto3). installer.py 가 만든 AWS 리소스를 **모두** 지운다. agentic-work/uninstaller.py 의 구조를 따른다.

기본은 전체 삭제다(옵션 불필요). 남기고 싶은 것만 --keep-* 로 지정한다.

  지우는 것  CloudFront, ECS(서비스·클러스터·태스크 정의·로그), ALB·타깃 그룹, ECR, VPC·서브넷·보안 그룹,
             Secrets Manager 시크릿, 승인 알림(EventBridge 규칙 + Lambda), Gateway·interceptor Lambda,
             MCP 테스트 harness `ai_portal`, IAM 역할, 사용자 정의 도메인의 DNS 레코드·인증서,
             DynamoDB 테이블 `ai-portal`(등록 항목·사용 승인·로그인 기록), AgentCore Registry `ai-portal`(record 전부)
             + SSM 파라미터(관리자·reviewer·정책), Cognito 사용자 풀 `ai-portal-users`,
             포털의 '에이전트 생성'으로 만든 harness(Project=ai-portal 태그)
  지우지 않음 Prompt Management 의 프롬프트(콘솔에서 직접 정리)

  --keep-table / --keep-registry / --keep-cognito / --keep-agents   해당 데이터 리소스를 남긴다
  --keep-dns                                                         Route53 레코드·ACM 인증서를 남긴다

사용:
  python uninstaller.py                  # 확인 후 전체 삭제
  python uninstaller.py --yes            # 확인 없이 전체 삭제
  python uninstaller.py --dry-run        # 지울 대상만 보여 준다(아무것도 지우지 않는다)
  python uninstaller.py --keep-table --keep-registry   # 데이터는 남기고 인프라만 삭제
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, WaiterError

# ── Configuration (installer.py 와 같아야 한다) ─────────────────────
project_name = "ai-portal"
region = "us-west-2"
WEBSEARCH_REGION = "us-east-1"            # websearch gateway · Cognito 의 리전
COGNITO_REGION = WEBSEARCH_REGION
DOMAIN = "portal.my-agentic-ai.click"
DNS_PROFILE = "stock"                     # 이 도메인의 Route53 호스팅 영역이 있는 AWS CLI 프로파일
DNS_ZONE_NAME = "my-agentic-ai.click."
ACM_REGION = "us-east-1"
DISTRIBUTION_COMMENTS = ("AI Portal", f"{project_name} portal")

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "application" / "config.json"

DDB_TABLE = project_name
NOTIFY_FUNCTION = f"{project_name}-approval-notify"
NOTIFY_ROLE = f"role-lambda-notify-for-{project_name}-{region}"
APPROVAL_RULE = f"{project_name}-approval-requested"
INTERCEPTOR_FUNCTION = f"lambda-{project_name}-intercepter"
COGNITO_POOL_NAME = f"{project_name}-users"
SSM_PARAMS = [f"/{project_name}/admins", f"/{project_name}/reviewers", f"/{project_name}/policy"]

# (gateway 이름, 리전). installer.ensure_gateways() 가 만드는 세 개
GATEWAYS = [(f"gw-{project_name}", region),
            (f"gw-{project_name}-websearch-jwt", WEBSEARCH_REGION),
            (f"gw-{project_name}-websearch-iam", WEBSEARCH_REGION)]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("uninstaller")

session = boto3.Session(region_name=region)
account_id = session.client("sts").get_caller_identity()["Account"]
ec2 = session.client("ec2")
iam = session.client("iam")
ecr = session.client("ecr")
ecs = session.client("ecs")
elbv2 = session.client("elbv2")
logs = session.client("logs")
secretsmanager = session.client("secretsmanager")
cloudfront = session.client("cloudfront")
events = session.client("events")
lam = session.client("lambda")
ssm = session.client("ssm")
agentcore = session.client("bedrock-agentcore-control")
agent_registry = session.client("agent-registry-control")   # Registry (installer.py 와 같은 네임스페이스)

DRY = False


def _code(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")


def _gone(e: ClientError) -> bool:
    c = _code(e)
    return c.endswith("NotFound") or c.endswith("NotFoundException") or c in (
        "NoSuchEntity", "NoSuchDistribution", "ResourceNotFoundException", "RepositoryNotFoundException",
        "ClusterNotFoundException", "ServiceNotFoundException", "LoadBalancerNotFound", "TargetGroupNotFound",
        "ParameterNotFound", "NoSuchHostedZone")


def attempt(what: str, fn: Callable[[], Any]) -> bool:
    """한 단계를 실행한다. 이미 없으면 넘어가고, 실패하면 경고만 남기고 다음 단계로 간다(부분 설치도 지울 수 있게)."""
    if DRY:
        logger.info("  [dry-run] %s", what)
        return True
    try:
        fn()
        logger.info("  ✓ %s", what)
        return True
    except ClientError as e:
        if _gone(e):
            logger.info("  - 이미 없음: %s", what)
            return True
        logger.warning("  ✗ %s: %s", what, e)
        return False
    except (WaiterError, BotoCoreError) as e:                     # 대기 시간 초과·네트워크 오류도 다음 단계로 넘어간다
        logger.warning("  ✗ %s: %s", what, e)
        return False


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _paginate(fn: Callable[..., Dict[str, Any]], key: str, **kw: Any) -> List[Any]:
    out, token = [], None
    while True:
        r = fn(**kw, **({"nextToken": token} if token else {}))
        out += r.get(key, [])
        token = r.get("nextToken")
        if not token:
            return out


# ── 1. CloudFront + 도메인 ──────────────────────────────────────────
def _our_distributions() -> List[Dict[str, Any]]:
    out = []
    for page in cloudfront.get_paginator("list_distributions").paginate():
        out += [d for d in page.get("DistributionList", {}).get("Items", []) or []
                if d.get("Comment") in DISTRIBUTION_COMMENTS]
    return out


def disable_cloudfront() -> None:
    """배포를 비활성화한다(삭제는 전파 후에 해야 해서 맨 뒤 delete_cloudfront 에서 한다)."""
    logger.info("[1] CloudFront 비활성화")
    for d in _our_distributions():
        def go(d=d) -> None:
            cur = cloudfront.get_distribution_config(Id=d["Id"])
            cfg = cur["DistributionConfig"]
            if not cfg.get("Enabled"):
                return
            cfg["Enabled"] = False
            cloudfront.update_distribution(Id=d["Id"], IfMatch=cur["ETag"], DistributionConfig=cfg)
        attempt(f"CloudFront {d['Id']} 비활성화", go)


def delete_cloudfront() -> None:
    logger.info("[9] CloudFront 삭제 (전파 대기)")
    for d in _our_distributions():
        if DRY:
            logger.info("  [dry-run] CloudFront %s 삭제", d["Id"])
            continue
        try:
            cloudfront.get_waiter("distribution_deployed").wait(Id=d["Id"], WaiterConfig={"Delay": 20, "MaxAttempts": 45})
        except Exception as e:                                    # noqa: BLE001 — 대기 실패는 삭제 시도에서 다시 드러난다
            logger.warning("  배포 전파 대기 실패: %s", e)

        def go(d=d) -> None:
            cloudfront.delete_distribution(Id=d["Id"], IfMatch=cloudfront.get_distribution_config(Id=d["Id"])["ETag"])
        attempt(f"CloudFront {d['Id']} 삭제", go)


def delete_domain(keep_dns: bool, certificate: bool) -> None:
    """사용자 정의 도메인. certificate=False: Route53 레코드(A/AAAA 별칭·검증 CNAME). True: ACM 인증서.
    인증서는 CloudFront 가 사라진 뒤에야 지워지므로 두 번에 나눠 호출한다. Route53 은 별도 프로파일에 있다."""
    logger.info("[8] 사용자 정의 도메인 %s (%s)", "인증서" if certificate else "DNS", DOMAIN)
    if keep_dns:
        logger.info("  --keep-dns: 건너뜀")
        return
    try:
        r53 = boto3.Session(profile_name=DNS_PROFILE).client("route53")
        zones = [z for z in r53.list_hosted_zones()["HostedZones"] if z["Name"] == DNS_ZONE_NAME and not z["Config"]["PrivateZone"]]
    except Exception as e:                                        # noqa: BLE001 — 프로파일이 없으면 DNS 는 수동으로 정리
        logger.warning("  Route53 접근 실패(프로파일 %r): %s — DNS 레코드는 직접 지우세요", DNS_PROFILE, e)
        r53, zones = None, []
    acm = session.client("acm", region_name=ACM_REGION)
    certs = [c["CertificateArn"] for page in acm.get_paginator("list_certificates").paginate()
             for c in page["CertificateSummaryList"] if c["DomainName"] == DOMAIN]
    if not certificate and r53 and zones:
        zone = zones[0]["Id"].split("/")[-1]
        names = {f"{DOMAIN}."}
        for arn in certs:                                         # 검증용 CNAME
            for o in acm.describe_certificate(CertificateArn=arn)["Certificate"].get("DomainValidationOptions", []):
                if o.get("ResourceRecord"):
                    names.add(o["ResourceRecord"]["Name"])
        recs = [rr for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone)
                for rr in page["ResourceRecordSets"] if rr["Name"] in names and rr["Type"] in ("A", "AAAA", "CNAME")]
        for rr in recs:
            attempt(f"DNS {rr['Type']} {rr['Name']}", lambda rr=rr: r53.change_resource_record_sets(
                HostedZoneId=zone, ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rr}]}))
    for arn in certs if certificate else []:
        attempt(f"ACM 인증서 {arn.rsplit('/', 1)[-1]}", lambda arn=arn: acm.delete_certificate(CertificateArn=arn))


def _cluster_exists(name: str) -> bool:
    c = ecs.describe_clusters(clusters=[name])["clusters"]
    return bool(c) and c[0]["status"] == "ACTIVE"


# ── 2. ECS / ECR / 로그 ─────────────────────────────────────────────
def delete_ecs() -> None:
    logger.info("[2] ECS / ECR / 로그")
    cluster, svc = f"cluster-for-{project_name}", f"service-for-{project_name}"

    def stop_service() -> None:
        s = ecs.describe_services(cluster=cluster, services=[svc])["services"]
        if s and s[0]["status"] != "INACTIVE":
            ecs.update_service(cluster=cluster, service=svc, desiredCount=0)
            ecs.delete_service(cluster=cluster, service=svc, force=True)
    attempt(f"ECS 서비스 {svc}", stop_service)
    if not DRY:                                                   # 서비스가 INACTIVE 가 되고 태스크가 멈춰야 클러스터를 지울 수 있다
        for _ in range(60):
            s = ecs.describe_services(cluster=cluster, services=[svc])["services"] if _cluster_exists(cluster) else []
            running = ecs.list_tasks(cluster=cluster)["taskArns"] if _cluster_exists(cluster) else []
            if (not s or s[0]["status"] == "INACTIVE") and not running:
                break
            time.sleep(10)
    attempt(f"ECS 클러스터 {cluster}", lambda: ecs.delete_cluster(cluster=cluster))
    try:
        arns = [a for page in ecs.get_paginator("list_task_definitions").paginate(familyPrefix=f"task-for-{project_name}")
                for a in page["taskDefinitionArns"]]
    except ClientError:
        arns = []
    for a in arns:
        attempt(f"태스크 정의 {a.rsplit('/', 1)[-1]}", lambda a=a: ecs.deregister_task_definition(taskDefinition=a))
    if arns and not DRY:
        for i in range(0, len(arns), 10):
            try:
                ecs.delete_task_definitions(taskDefinitions=arns[i:i + 10])
            except ClientError as e:
                logger.info("  태스크 정의 완전 삭제는 건너뜀: %s", _code(e))
    attempt(f"로그 그룹 /ecs/{project_name}", lambda: logs.delete_log_group(logGroupName=f"/ecs/{project_name}"))
    attempt(f"ECR 저장소 {project_name}", lambda: ecr.delete_repository(repositoryName=project_name, force=True))


# ── 3. ALB ──────────────────────────────────────────────────────────
def delete_alb() -> None:
    logger.info("[3] ALB / 타깃 그룹")
    name = f"alb-for-{project_name}"
    try:
        lb = elbv2.describe_load_balancers(Names=[name])["LoadBalancers"][0]
    except ClientError as e:
        if not _gone(e):
            raise
        lb = None
    if lb:
        for l in elbv2.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]:
            attempt(f"리스너 {l['Port']}", lambda l=l: elbv2.delete_listener(ListenerArn=l["ListenerArn"]))
        attempt(f"ALB {name}", lambda: elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"]))
        if not DRY:
            try:
                elbv2.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
            except Exception as e:                                # noqa: BLE001
                logger.warning("  ALB 삭제 대기 실패: %s", e)
    tg_name = f"tg-for-{project_name}"[:32]
    try:
        for tg in elbv2.describe_target_groups(Names=[tg_name])["TargetGroups"]:
            attempt(f"타깃 그룹 {tg_name}", lambda tg=tg: elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"]))
    except ClientError as e:
        if not _gone(e):
            raise


# ── 4. VPC ──────────────────────────────────────────────────────────
def delete_vpc() -> None:
    logger.info("[4] VPC / 서브넷 / 보안 그룹")
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [f"vpc-for-{project_name}"]}])["Vpcs"]
    if not vpcs:
        logger.info("  VPC 없음")
        return
    vpc_id = vpcs[0]["VpcId"]
    vpc_f = [{"Name": "vpc-id", "Values": [vpc_id]}]
    if not DRY:                                                   # ECS 태스크 ENI 가 풀리길 기다린다
        for _ in range(30):
            if not ec2.describe_network_interfaces(Filters=vpc_f)["NetworkInterfaces"]:
                break
            time.sleep(10)
    sgs = [g for g in ec2.describe_security_groups(Filters=vpc_f)["SecurityGroups"] if g["GroupName"] != "default"]
    for g in sgs:                                                 # 보안 그룹끼리의 참조 규칙을 먼저 푼다
        for kind, perms in (("ingress", g["IpPermissions"]), ("egress", g["IpPermissionsEgress"])):
            if perms and not DRY:
                try:
                    (ec2.revoke_security_group_ingress if kind == "ingress" else ec2.revoke_security_group_egress)(
                        GroupId=g["GroupId"], IpPermissions=perms)
                except ClientError as e:
                    logger.info("  규칙 해제 건너뜀 %s: %s", g["GroupName"], _code(e))
    for g in sgs:
        attempt(f"보안 그룹 {g['GroupName']}", lambda g=g: ec2.delete_security_group(GroupId=g["GroupId"]))
    for sn in ec2.describe_subnets(Filters=vpc_f)["Subnets"]:
        attempt(f"서브넷 {sn['SubnetId']}", lambda sn=sn: ec2.delete_subnet(SubnetId=sn["SubnetId"]))
    for rt in ec2.describe_route_tables(Filters=vpc_f)["RouteTables"]:
        if any(a.get("Main") for a in rt["Associations"]):
            continue
        for a in rt["Associations"]:
            attempt(f"라우트 테이블 연결 {a['RouteTableAssociationId']}",
                    lambda a=a: ec2.disassociate_route_table(AssociationId=a["RouteTableAssociationId"]))
        attempt(f"라우트 테이블 {rt['RouteTableId']}", lambda rt=rt: ec2.delete_route_table(RouteTableId=rt["RouteTableId"]))
    for igw in ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}])["InternetGateways"]:
        attempt(f"IGW 분리 {igw['InternetGatewayId']}",
                lambda igw=igw: ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id))
        attempt(f"IGW {igw['InternetGatewayId']}", lambda igw=igw: ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"]))
    attempt(f"VPC {vpc_id}", lambda: ec2.delete_vpc(VpcId=vpc_id))


# ── 5. 승인 알림 / Gateway / Lambda ─────────────────────────────────
def delete_notifications() -> None:
    logger.info("[5] 승인 요청 알림 (EventBridge 규칙 + Lambda)")
    try:
        ids = [t["Id"] for t in events.list_targets_by_rule(Rule=APPROVAL_RULE).get("Targets", [])]
        if ids:
            attempt("규칙 타깃 제거", lambda: events.remove_targets(Rule=APPROVAL_RULE, Ids=ids))
    except ClientError as e:
        if not _gone(e):
            raise
    attempt(f"EventBridge 규칙 {APPROVAL_RULE}", lambda: events.delete_rule(Name=APPROVAL_RULE))
    attempt(f"Lambda {NOTIFY_FUNCTION}", lambda: lam.delete_function(FunctionName=NOTIFY_FUNCTION))
    sns = session.client("sns")                                   # 예전 설치가 만든 SNS 토픽(현재 인스톨러는 만들지 않는다)
    attempt(f"SNS 토픽 {project_name}-approval-requests", lambda: sns.delete_topic(
        TopicArn=f"arn:aws:sns:{region}:{account_id}:{project_name}-approval-requests"))


def _gateways_in(ac: Any, names: List[str]) -> List[Dict[str, Any]]:
    return [g for g in _paginate(ac.list_gateways, "items") if g["name"] in names]


def delete_gateways() -> None:
    logger.info("[6] AgentCore Gateway / interceptor Lambda")
    for rgn in sorted({r for _, r in GATEWAYS}):
        ac = session.client("bedrock-agentcore-control", region_name=rgn)
        for gw in _gateways_in(ac, [n for n, r in GATEWAYS if r == rgn]):
            gid = gw["gatewayId"]
            try:
                targets = _paginate(ac.list_gateway_targets, "items", gatewayIdentifier=gid)
            except ClientError:
                targets = []
            for t in targets:
                attempt(f"[{rgn}] target {t['name']}", lambda t=t: ac.delete_gateway_target(gatewayIdentifier=gid, targetId=t["targetId"]))
            if targets and not DRY:                               # target 삭제가 끝나야 gateway 를 지울 수 있다
                for _ in range(30):
                    if not _paginate(ac.list_gateway_targets, "items", gatewayIdentifier=gid):
                        break
                    time.sleep(5)
            attempt(f"[{rgn}] gateway {gw['name']}", lambda gid=gid: ac.delete_gateway(gatewayIdentifier=gid))
        rl = session.client("lambda", region_name=rgn)
        attempt(f"[{rgn}] Lambda {INTERCEPTOR_FUNCTION}", lambda rl=rl: rl.delete_function(FunctionName=INTERCEPTOR_FUNCTION))


# ── 6b. Harness ─────────────────────────────────────────────────────
def _harnesses() -> List[Dict[str, Any]]:
    return _paginate(agentcore.list_harnesses, "harnesses")


def delete_test_harness() -> None:
    """MCP 테스트용 harness `ai_portal`. installer 가 만든 것이라 항상 지운다."""
    logger.info("[7] MCP 테스트 harness (ai_portal)")
    api_name = project_name.replace("-", "_")
    for h in _harnesses():
        if h.get("harnessName") == api_name:
            attempt(f"harness {api_name}", lambda h=h: agentcore.delete_harness(harnessId=h["harnessId"]))


def delete_user_agents() -> None:
    """포털의 '에이전트 생성'으로 만든 harness. create_harness 가 Project=ai-portal 태그를 붙인다."""
    logger.info("[7b] 포털에서 만든 에이전트(harness)")
    api_name = project_name.replace("-", "_")
    for h in _harnesses():
        if h.get("harnessName") == api_name:
            continue                                              # 테스트 harness 는 delete_test_harness 가 처리
        try:
            tags = agentcore.list_tags_for_resource(resourceArn=h["arn"]).get("tags", {})
        except ClientError:
            tags = {}
        if tags.get("Project") == project_name:
            attempt(f"harness {h['harnessName']}", lambda h=h: agentcore.delete_harness(harnessId=h["harnessId"]))
        else:
            logger.info("  유지: %s (Project 태그 없음)", h["harnessName"])


# ── 7. Registry / DynamoDB / Cognito / Secrets ──────────────────────
def delete_registry() -> None:
    """Registry `ai-portal` 과 record 전부. 설치된 시점에 따라 구 bedrock-agentcore 네임스페이스 또는 agent-registry
    네임스페이스에 있을 수 있어 둘 다 찾는다."""
    logger.info("[R] Registry '%s' (record 전부 포함)", project_name)
    found = False
    for label, cl in (("agent-registry", agent_registry), ("bedrock-agentcore", agentcore)):
        try:
            reg = next((r for r in _paginate(cl.list_registries, "registries") if r["name"] == project_name), None)
        except (ClientError, BotoCoreError) as e:                 # 종료된 네임스페이스는 호출 자체가 실패할 수 있다
            logger.info("  %s 네임스페이스 조회 건너뜀: %s", label, e)
            continue
        if not reg:
            continue
        found = True
        rid = reg["registryId"]
        logger.info("  %s 네임스페이스에서 발견: %s", label, rid)
        for rec in _paginate(cl.list_registry_records, "registryRecords", registryId=rid):
            attempt(f"record {rec.get('name', rec['recordId'])}",
                    lambda rec=rec, cl=cl, rid=rid: cl.delete_registry_record(registryId=rid, recordId=rec["recordId"]))
        if not DRY:                                               # record 삭제 반영 대기
            for _ in range(30):
                if not _paginate(cl.list_registry_records, "registryRecords", registryId=rid):
                    break
                time.sleep(5)
        attempt(f"Registry {project_name} ({label})", lambda cl=cl, rid=rid: cl.delete_registry(registryId=rid))
    if not found:
        logger.info("  Registry 없음")
    for p in SSM_PARAMS:
        attempt(f"SSM {p}", lambda p=p: ssm.delete_parameter(Name=p))


def delete_table() -> None:
    logger.info("[T] DynamoDB 테이블 '%s'", DDB_TABLE)
    ddb = session.client("dynamodb")
    try:
        ddb.describe_table(TableName=DDB_TABLE)
    except ClientError as e:
        if _gone(e):
            logger.info("  테이블 없음")
            return
        raise
    attempt("삭제 보호 해제", lambda: ddb.update_table(TableName=DDB_TABLE, DeletionProtectionEnabled=False))
    attempt(f"테이블 {DDB_TABLE}", lambda: ddb.delete_table(TableName=DDB_TABLE))


def delete_cognito() -> None:
    logger.info("[C] Cognito 사용자 풀 '%s' (%s)", COGNITO_POOL_NAME, COGNITO_REGION)
    cg = session.client("cognito-idp", region_name=COGNITO_REGION)
    pool = next((p for p in cg.list_user_pools(MaxResults=60)["UserPools"] if p["Name"] == COGNITO_POOL_NAME), None)
    if not pool:
        logger.info("  사용자 풀 없음")
        return
    pid = pool["Id"]
    desc = cg.describe_user_pool(UserPoolId=pid)["UserPool"]
    if desc.get("Domain"):                                        # 도메인을 먼저 지워야 풀을 지울 수 있다
        attempt(f"도메인 {desc['Domain']}", lambda: cg.delete_user_pool_domain(Domain=desc["Domain"], UserPoolId=pid))
    if desc.get("DeletionProtection") == "ACTIVE":
        attempt("삭제 보호 해제", lambda: cg.update_user_pool(UserPoolId=pid, DeletionProtection="INACTIVE"))
    attempt(f"사용자 풀 {pid}", lambda: cg.delete_user_pool(UserPoolId=pid))


def delete_secrets() -> None:
    logger.info("[S] Secrets Manager")
    for name in (f"{project_name}/session-signing-key", f"{project_name}/llm-gateway-key", f"{project_name}/alb-origin-header"):
        attempt(f"시크릿 {name}", lambda name=name: secretsmanager.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True))


# ── IAM ─────────────────────────────────────────────────────────────
def delete_roles() -> None:
    logger.info("[I] IAM 역할")
    names = [f"role-ecs-task-for-{project_name}-{region}", f"role-ecs-execution-for-{project_name}-{region}",
             f"role-harness-for-{project_name}-{region}", NOTIFY_ROLE]
    for rgn in sorted({region, WEBSEARCH_REGION}):
        names += [f"role-lambda-interceptor-for-{project_name}-{rgn}", f"role-gateway-for-{project_name}-{rgn}"]
    for name in names:
        def go(name=name) -> None:
            for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
            iam.delete_role(RoleName=name)
        attempt(f"역할 {name}", go)


def delete_local_config() -> None:
    logger.info("[L] 로컬 설정")
    if CONFIG_PATH.exists() and not DRY:
        CONFIG_PATH.unlink()
        logger.info("  ✓ 삭제: %s", CONFIG_PATH)


# ── main ────────────────────────────────────────────────────────────
def main() -> None:
    global DRY
    ap = argparse.ArgumentParser(description="AI Portal uninstaller")
    ap.add_argument("--yes", action="store_true", help="확인 없이 진행")
    ap.add_argument("--dry-run", action="store_true", help="지울 대상만 보여 주고 아무것도 지우지 않는다")
    ap.add_argument("--keep-table", action="store_true", help="DynamoDB 테이블(등록 항목·사용 승인·로그인 기록)을 남긴다")
    ap.add_argument("--keep-registry", action="store_true", help="AgentCore Registry 와 record, SSM 파라미터를 남긴다")
    ap.add_argument("--keep-cognito", action="store_true", help="Cognito 사용자 풀을 남긴다")
    ap.add_argument("--keep-agents", action="store_true", help="포털에서 만든 에이전트(Project=ai-portal 태그 harness)를 남긴다")
    ap.add_argument("--keep-dns", action="store_true", help="Route53 레코드·ACM 인증서를 남긴다")
    args = ap.parse_args()
    DRY = args.dry_run

    logger.info("=" * 60)
    logger.info("Project=%s Region=%s Account=%s%s", project_name, region, account_id, "  [DRY RUN]" if DRY else "")
    keep = [n for n, f in (("DynamoDB 테이블", args.keep_table), ("Registry", args.keep_registry),
                           ("Cognito 사용자 풀", args.keep_cognito), ("포털에서 만든 에이전트", args.keep_agents)) if f]
    wipe = [n for n in ("DynamoDB 테이블(등록 항목·사용 승인·로그인 기록)", "Registry 와 record 전부", "Cognito 사용자 풀",
                        "포털에서 만든 에이전트(harness)") if not any(n.startswith(k) for k in keep)]
    logger.info("삭제 대상: 인프라 전체 + %s", ", ".join(wipe) or "(데이터 리소스 없음)")
    if keep:
        logger.info("보존(--keep-*): %s", ", ".join(keep))
    logger.info("=" * 60)
    if not args.yes and not DRY:
        logger.warning("되돌릴 수 없습니다. 위 데이터 리소스까지 모두 사라집니다.")
        if input("\n위 계정에서 AI Portal 을 전부 삭제합니다. 계속할까요? (yes/no): ").strip().lower() != "yes":
            logger.info("취소했습니다.")
            sys.exit(0)

    t0 = time.time()
    disable_cloudfront()                  # 먼저 끈다 — 전파되는 동안 나머지를 지운다
    delete_ecs()
    delete_alb()
    delete_notifications()
    delete_gateways()
    delete_test_harness()
    if not args.keep_agents:
        delete_user_agents()
    if not args.keep_registry:
        delete_registry()
    if not args.keep_cognito:
        delete_cognito()
    if not args.keep_table:
        delete_table()
    delete_secrets()
    delete_vpc()
    delete_domain(args.keep_dns, certificate=False)
    delete_cloudfront()                   # 전파가 끝난 뒤 삭제
    delete_domain(args.keep_dns, certificate=True)    # 인증서는 CloudFront 가 사라진 뒤에야 지워진다
    delete_roles()
    if not (args.keep_table or args.keep_registry or args.keep_cognito):
        delete_local_config()
    logger.info("=" * 60)
    logger.info("✓ %s (%.0f초)", "dry-run 완료" if DRY else "인프라 삭제 완료", time.time() - t0)
    if keep:
        logger.info("남겨 둔 리소스: %s", ", ".join(keep))
    if not DRY:
        logger.info("남은 리소스는 콘솔에서 Project=%s 태그로 확인하세요.", project_name)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
