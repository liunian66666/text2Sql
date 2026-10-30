"""RAG-enhanced Text-to-SQL tool."""
from typing import Annotated, Any, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState


def create_text2sql_tool(registry):
    @tool("run_text_to_sql")
    def run_text_to_sql(question: str,
                        intent: Literal["query", "ranking", "trend", "proportion"],
                        tool_call_id: Annotated[str, InjectedToolCallId],
                        state: Annotated[dict[str, Any], InjectedState] = None,
                        config: RunnableConfig = None):
        """调用 RAG Text-to-SQL Skill 生成、修订、选择并只读执行 SQL。"""
        return registry.invoke("run_text_to_sql", {"question": question, "intent": intent},
                               state, config, tool_call_id)

    return run_text_to_sql
