import asyncio
from pathlib import Path
import tempfile
import unittest

from app.mcp_server import create_mcp_server


class MCPServerTest(unittest.TestCase):
    def test_exposes_read_only_analysis_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = create_mcp_server(sql_skill=object(), databases={"demo": Path(tmp) / "demo.sqlite"})
            tools = asyncio.run(server.list_tools())
            names = {tool.name for tool in tools}
            self.assertEqual(names, {
                "list_databases", "search_business_metrics",
                "inspect_database_schema", "run_text_to_sql",
            })


if __name__ == "__main__":
    unittest.main()
