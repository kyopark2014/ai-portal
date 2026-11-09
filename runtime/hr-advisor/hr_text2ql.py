"""HR text-to-SQL helpers: Bedrock으로 SQL 생성, Aurora Data API로 실행."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any

import boto3

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("hr-text2ql")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SCHEMA_PATH = os.path.join(SCRIPT_DIR, "hr_schema.json")

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}


_config = _load_json(CONFIG_PATH)
_schema = _load_json(SCHEMA_PATH)


def _cfg(key: str, *env_keys: str, default: str = "") -> str:
    for ek in env_keys:
        val = os.environ.get(ek)
        if val:
            return val
    return str(_config.get(key) or default)


REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or _config.get("region")
    or "us-west-2"
)
CLUSTER_ARN = _cfg(
    "hr_aurora_cluster_arn",
    "AURORA_CLUSTER_ARN",
    "HR_AURORA_CLUSTER_ARN",
)
SECRET_ARN = _cfg(
    "hr_aurora_secret_arn",
    "AURORA_SECRET_ARN",
    "HR_AURORA_SECRET_ARN",
)
DATABASE = _cfg(
    "hr_aurora_database",
    "AURORA_DATABASE",
    "HR_AURORA_DATABASE",
    default="hrdb",
)
MODEL_ID = _cfg(
    "hr_advisor_model_id",
    "BEDROCK_MODEL_ID",
    "MODEL_ID",
    default=DEFAULT_MODEL_ID,
)

logger.info(
    "hr-text2ql config: region=%s database=%s cluster=%s model=%s",
    REGION,
    DATABASE,
    (CLUSTER_ARN[:60] + "...") if len(CLUSTER_ARN) > 60 else (CLUSTER_ARN or "(missing)"),
    MODEL_ID,
)


def _bedrock():
    return boto3.client("bedrock-runtime", region_name=REGION)


def _rds_data():
    return boto3.client("rds-data", region_name=REGION)


def load_schema_description() -> dict:
    return _schema or _load_json(SCHEMA_PATH)


def extract_sql_query(content: str) -> str:
    """<result> 또는 응답 본문에서 실행 가능한 SQL만 추출."""
    text = content or ""
    if "<result>" in text and "</result>" in text:
        start = text.find("<result>") + len("<result>")
        end = text.find("</result>", start)
        text = text[start:end].strip()
    else:
        text = text.strip()

    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()

    lines = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("--")
    ]
    text = "\n".join(lines).strip()

    if not text or text.lstrip().startswith("--"):
        flat = re.sub(r"\s+", " ", content or "")
        match = re.search(
            r"\b(SELECT|WITH|INSERT|UPDATE|DELETE)\b", flat, re.IGNORECASE
        )
        if match:
            text = flat[match.start() :].strip()

    return text.rstrip(";").strip()


def generate_query(question: str) -> str:
    """자연어 질문 → PostgreSQL SELECT."""
    schema = load_schema_description()
    samples = schema.get("sample_queries") or []
    dialect = schema.get("dialect") or "postgresql"

    system = (
        "당신은 사용자 질문에 대한 PostgreSQL SQL 쿼리를 작성하는 유능한 데이터베이스 "
        "엔지니어입니다. HR employees 테이블만 사용하세요. "
        "SELECT/WITH 조회만 작성하고 INSERT/UPDATE/DELETE는 금지합니다. "
        "<result> 태그 안에는 실행 가능한 SQL 문장만 넣으세요. "
        "주석, 설명, 마크다운 코드블록은 <result> 안에 포함하지 마세요."
    )
    human = (
        f"질문: {question}\n"
        f"방언: {dialect}\n"
        f"샘플 쿼리: {json.dumps(samples, ensure_ascii=False)}\n"
        f"스키마: {json.dumps(schema.get('tables') or schema, ensure_ascii=False)}\n\n"
        "응답 형식:\n"
        "- <result> 태그 안: SELECT/WITH 로 시작하는 SQL 한 문장만\n"
        "- <result> 태그 밖: 필요할 때만 짧은 설명"
    )

    response = _bedrock().converse(
        modelId=MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": human}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0},
    )
    parts = response.get("output", {}).get("message", {}).get("content") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    sql = extract_sql_query(text)
    logger.info("generated_query: %s", sql)
    return sql


def _cell_value(field: dict[str, Any]) -> Any:
    if not field:
        return None
    if field.get("isNull"):
        return None
    for key in (
        "stringValue",
        "longValue",
        "doubleValue",
        "booleanValue",
        "blobValue",
    ):
        if key in field:
            return field[key]
    if "arrayValue" in field:
        return field["arrayValue"]
    return field


def _format_records(response: dict[str, Any]) -> str:
    columns = [
        c.get("name") or f"col{i}"
        for i, c in enumerate(response.get("columnMetadata") or [])
    ]
    rows: list[dict[str, Any]] = []
    for record in response.get("records") or []:
        if columns and len(columns) == len(record):
            rows.append(
                {columns[i]: _cell_value(cell) for i, cell in enumerate(record)}
            )
        else:
            rows.append({"values": [_cell_value(cell) for cell in record]})

    if not rows and response.get("numberOfRecordsUpdated") is not None:
        return json.dumps(
            {"numberOfRecordsUpdated": response["numberOfRecordsUpdated"]},
            ensure_ascii=False,
        )
    return json.dumps(rows, ensure_ascii=False, default=str)


def is_execution_error(result: str) -> bool:
    return isinstance(result, str) and result.startswith(
        "An error occurred while executing the query:"
    )


def _assert_read_only(sql: str) -> None:
    cleaned = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    cleaned = re.sub(r"--.*?$", " ", cleaned, flags=re.MULTILINE).strip()
    first = cleaned.split(None, 1)[0].upper() if cleaned else ""
    if first not in ("SELECT", "WITH"):
        raise ValueError(f"Only SELECT/WITH queries are allowed (got {first or 'empty'})")


def execute_query(sql: str) -> str:
    """Aurora Data API로 SQL 실행."""
    if not CLUSTER_ARN or not SECRET_ARN:
        return (
            "An error occurred while executing the query: "
            "Aurora cluster/secret ARN is not configured "
            "(set hr_aurora_cluster_arn / hr_aurora_secret_arn in config.json)"
        )
    try:
        _assert_read_only(sql)
        response = _rds_data().execute_statement(
            resourceArn=CLUSTER_ARN,
            secretArn=SECRET_ARN,
            database=DATABASE,
            sql=sql.rstrip(";"),
            includeResultMetadata=True,
        )
        result = _format_records(response)
        logger.info("query result rows≈%s", len(response.get("records") or []))
        return result
    except Exception as e:
        logger.error("execute_query failed: %s", e)
        return f"An error occurred while executing the query: {e}"


def query(question: str) -> dict[str, Any]:
    """자연어 → SQL 생성 + 실행."""
    sql = generate_query(question)
    result = execute_query(sql)
    return {"question": question, "sql": sql, "result": result}
