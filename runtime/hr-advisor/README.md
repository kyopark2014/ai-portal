# HR Advisor MCP (AgentCore Runtime)

Aurora PostgreSQL에 적재된 HR 임직원 정보를 자연어(text-to-SQL)로 조회하는
IAM 인증 MCP입니다. `installer_hr.py`가 Aurora를 만들고 샘플 CSV를 넣은 뒤
Docker 이미지를 ECR에 푸시하고 AgentCore Runtime(MCP)으로 배포합니다.

## Local

```bash
# installer가 만든 값을 담은 config.json 필요
cp ../../application/config.json .
# 또는: export AURORA_CLUSTER_ARN=... AURORA_SECRET_ARN=... AURORA_DATABASE=hrdb
python -m mcp_server_hr_advisor
```

## Runtime env / config.json

| 키 / 변수 | 설명 |
|-----------|------|
| `hr_aurora_cluster_arn` / `AURORA_CLUSTER_ARN` | Aurora cluster ARN |
| `hr_aurora_secret_arn` / `AURORA_SECRET_ARN` | Secrets Manager master secret ARN |
| `hr_aurora_database` / `AURORA_DATABASE` | DB 이름 (기본 `hrdb`) |
| `hr_aurora_cluster_id` | Cluster identifier |
| `region` / `AWS_REGION` | 리전 |
| `BEDROCK_MODEL_ID` | SQL 생성 모델 (기본 Claude Sonnet) |

## Tools

| Tool | 설명 |
|------|------|
| `query_hr(question)` | 자연어 → SQL 생성 + Aurora 실행 (권장) |
| `generate_query(question)` | SQL만 생성 |
| `execute_query(sql)` | SELECT/WITH만 Aurora Data API로 실행 |
