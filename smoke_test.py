"""Local smoke test for yargi-mcp: build the FastMCP app and list registered tools.

Run with:  uv run python smoke_test.py
This is a non-blocking check (does not start the stdio server loop).
"""
import sys

import mcp_server_main


def main() -> int:
    app = mcp_server_main.create_app()
    tools = list(app._tool_manager._tools.values())
    names = [t.name for t in tools]
    print("OK tools loaded:", len(tools))
    print("all tool names:", names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
