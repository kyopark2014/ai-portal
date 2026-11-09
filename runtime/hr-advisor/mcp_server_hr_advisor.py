"""Streamable-HTTP MCP server: HR 정보를 Aurora에서 text-to-SQL로 조회."""

from __future__ import annotations

import json
import logging
import sys

import hr_text2ql
from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("hr-advisor")

try:
    # mcp>=2.x: host/stateless_http 는 MCPServer() 가 아니라 run() 인자
    mcp = MCPServer(
        name="hr-advisor",
        instructions=(
            "You look up HR employee information stored in an Aurora PostgreSQL "
            "database (employees table). Prefer query_hr for natural-language "
            "questions; use generate_query / execute_query when the caller needs "
            "to inspect or adjust SQL. Only SELECT/WITH queries are allowed."
        ),
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
    logger.info("Error: %s", e)
    raise


@mcp.tool()
def generate_query(question: str) -> str:
    """
    Generate a PostgreSQL SELECT for the HR employees table.

    question: natural-language HR question (Korean or English)
    return: SQL statement only
    """
    logger.info("generate_query --> question=%s", question)
    try:
        sql = hr_text2ql.generate_query(question)
        logger.info("sql: %s", sql)
        return sql
    except Exception as e:
        logger.error("Error in generate_query: %s", e)
        return f"Error generating SQL: {e}"


@mcp.tool()
def execute_query(sql: str) -> str:
    """
    Execute a read-only SQL statement against the HR Aurora database.

    sql: SELECT/WITH statement generated for the employees table
    return: JSON rows, or an error message starting with
        'An error occurred while executing the query:'
    """
    logger.info("execute_query --> sql=%s", sql)
    try:
        result = hr_text2ql.execute_query(sql)
        logger.info("result length: %s", len(result) if result else 0)
        return result
    except Exception as e:
        logger.error("Error in execute_query: %s", e)
        return f"An error occurred while executing the query: {e}"


@mcp.tool()
def query_hr(question: str) -> str:
    """
    Answer an HR question by generating SQL and querying Aurora.

    question: natural-language question about employees
        (department headcount, employee lookup, tenure, etc.)
    return: JSON with question, sql, and result (or error)
    """
    logger.info("query_hr --> question=%s", question)
    try:
        payload = hr_text2ql.query(question)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        logger.info("query_hr done: sql=%s", payload.get("sql"))
        return text
    except Exception as e:
        logger.error("Error in query_hr: %s", e)
        return json.dumps(
            {"question": question, "error": str(e)},
            ensure_ascii=False,
        )


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000, stateless_http=True)
