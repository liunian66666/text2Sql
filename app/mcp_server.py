"""MCP transport for DeepEye's safe Text-to-SQL capabilities."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from app.agent.tools import AgentToolbox
from app.enterprise import EnterpriseKnowledgeBase


def create_mcp_server(*, sql_skill, databases: dict[str, Path]) -> MCPServer:
    knowledge = EnterpriseKnowledgeBase()
    toolbox = AgentToolbox(sql_skill, databases, knowledge)
    server = MCPServer(
        name="deepeye-sql",
        title="DeepEye SQL Analysis Tools",
        description="Read-only enterprise database discovery and RAG Text-to-SQL tools.",
        instructions="Use schema and business metric tools before run_text_to_sql. All database execution is read-only.",
    )

    @server.tool(description="List server-registered database IDs. Filesystem paths are never exposed.")
    def list_databases() -> list[str]:
        return sorted(databases)

    @server.tool(description="Retrieve enterprise metric definitions and SQL calculation rules.")
    def search_business_metrics(query: str, limit: int = 4) -> list[dict[str, Any]]:
        return knowledge.retrieve(query, limit=max(1, min(limit, 8)))

    @server.tool(description="Inspect real tables, columns, primary keys and foreign keys in a registered database.")
    def inspect_database_schema(database_id: str, table_names: list[str] | None = None) -> dict[str, Any]:
        return toolbox.inspect_database_schema(database_id, table_names)

    @server.tool(description="Run DeepEye's RAG-enhanced Text-to-SQL pipeline and execute the selected SQL read-only.")
    def run_text_to_sql(question: str, database_id: str, evidence: str = "", max_rows: int = 100,
                        sql_timeout_seconds: int = 30) -> dict[str, Any]:
        result = sql_skill.run({
            "question": question, "database_id": database_id, "evidence": evidence,
            "max_rows": max_rows, "sql_timeout_seconds": sql_timeout_seconds,
        }).model_dump()
        result.pop("artifacts_dir", None)
        return result

    return server
