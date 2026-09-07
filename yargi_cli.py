#!/usr/bin/env python3
"""yargi-cli: Command-line bridge for the yargi-mcp Turkish legal tools.

Calls the FastMCP tool functions directly (no MCP protocol on the wire) and
prints the result as JSON to stdout. Designed to be pipe-friendly:

    * stdout  -> only the tool result JSON  (pipe to jq / xargs / etc.)
    * stderr  -> all INFO/warning logs from the modules
    * errors  -> {"error": "..."} on stdout + non-zero exit code

Usage
-----
    yargi-cli list
    yargi-cli schema <tool>
    yargi-cli <tool> [--param value ...]
    yargi-cli <tool> --help

Examples
--------
    yargi-cli list
    yargi-cli schema search_btk_decisions
    yargi-cli search_btk_decisions --keywords "fis-ek" --pageSize 2
    yargi-cli search_btk_decisions --keywords "fis-ek" --pageSize 2 | jq '.decisions[0]'
    yargi-cli check_government_servers_health
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

# Configure logging BEFORE importing yargi-mcp so the module's own loggers
# attach to our root config. Keep stdout pristine for JSON output.
logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="%(levelname)s %(name)s: %(message)s",
)

import mcp_server_main as M  # noqa: E402  (import after logging config)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _json_schema_type(pschema: dict) -> str:
    """Normalize a JSON Schema property to a single concrete type string."""
    t = pschema.get("type")
    if isinstance(t, list):
        # e.g. ["string", "null"] -> first non-null
        t = next((x for x in t if x != "null"), "string")
    return t or "string"


def coerce_value(value: Any, schema: dict) -> Any:
    """Coerce a raw argparse value into the type required by the JSON Schema."""
    if value is None:
        return None
    t = _json_schema_type(schema)

    if t == "integer":
        if isinstance(value, bool):  # bool is subclass of int; guard it
            return int(value)
        return int(value)

    if t == "number":
        return float(value)

    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"boolean must be true/false, got {value!r}")

    if t == "array":
        items_schema = schema.get("items", {}) or {}
        item_enum = items_schema.get("enum")
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
        else:
            parts = list(value) if not isinstance(value, (str, bytes)) else [value]
        coerced = [coerce_value(p, items_schema) for p in parts]
        if item_enum is not None:
            bad = [p for p in coerced if p not in item_enum]
            if bad:
                raise ValueError(f"values {bad} not in allowed {item_enum}")
        return coerced

    # string (and fallback)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{value!r} not in allowed choices {schema['enum']}")
    return value


def serialize(result: Any) -> Any:
    """Make a tool result JSON-serializable (Pydantic models -> dict, etc.)."""
    if result is None:
        return None
    if hasattr(result, "model_dump"):  # Pydantic v2
        try:
            return result.model_dump()
        except Exception:
            pass
    if hasattr(result, "dict") and callable(getattr(result, "dict", None)):  # v1
        try:
            return result.dict()
        except Exception:
            pass
    return result  # primitive / dict / list / str


def build_arg_kwargs(pname: str, pschema: dict, required: bool) -> dict:
    """Build argparse add_argument kwargs for one JSON Schema property."""
    t = _json_schema_type(pschema)
    desc = pschema.get("description", "") or ""
    enum = pschema.get("enum")

    help_text = desc
    if enum:
        help_text += f"  [choices: {', '.join(map(str, enum))}]"
    if "default" in pschema:
        help_text += f"  (default: {pschema['default']!r})"

    kwargs: dict[str, Any] = {"help": help_text, "dest": pname}
    if required:
        kwargs["required"] = True

    if t == "boolean":
        kwargs["type"] = str
        if "default" in pschema:
            kwargs["default"] = str(pschema["default"]).lower()
    elif t == "array":
        kwargs["nargs"] = "+"
        item_enum = (pschema.get("items", {}) or {}).get("enum")
        if item_enum:
            kwargs["choices"] = item_enum
    elif enum:
        kwargs["choices"] = enum
        if "default" in pschema:
            kwargs["default"] = pschema["default"]
    else:
        if "default" in pschema:
            kwargs["default"] = pschema["default"]

    return kwargs


def build_parser(tools: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yargi-cli",
        description=(
            "CLI bridge for yargi-mcp Turkish legal tools. "
            "Every tool command prints JSON to stdout (pipe to jq)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("list", help="List all available tools (JSON array to stdout)")
    p_schema = sub.add_parser("schema", help="Print a tool's parameter JSON schema")
    p_schema.add_argument("tool", help="Tool name")

    # One subcommand per tool, arguments generated from its JSON schema.
    for name, tool in sorted(tools.items()):
        description = getattr(tool, "description", "") or ""
        tp = sub.add_parser(
            name,
            help=(description[:100] + ("..." if len(description) > 100 else "")),
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        params = (getattr(tool, "parameters", None) or {}).get("properties", {})
        required = set((getattr(tool, "parameters", None) or {}).get("required", []))
        for pname, pschema in params.items():
            kwargs = build_arg_kwargs(pname, pschema, pname in required)
            tp.add_argument(f"--{pname}", **kwargs)

    return parser


# --------------------------------------------------------------------------- #
# Core actions
# --------------------------------------------------------------------------- #
def cmd_list(tools: dict) -> int:
    out = [
        {"name": name, "description": getattr(tool, "description", "") or ""}
        for name, tool in sorted(tools.items())
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_schema(tools: dict, tool_name: str) -> int:
    if tool_name not in tools:
        print(json.dumps({"error": f"unknown tool: {tool_name!r}"}, ensure_ascii=False))
        return 1
    tool = tools[tool_name]
    payload = {
        "name": getattr(tool, "name", tool_name),
        "description": getattr(tool, "description", "") or "",
        "parameters": getattr(tool, "parameters", {}) or {},
        "output_schema": getattr(tool, "output_schema", None),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def collect_tool_kwargs(args: argparse.Namespace, tool) -> dict[str, Any]:
    params = (getattr(tool, "parameters", None) or {}).get("properties", {})
    kwargs: dict[str, Any] = {}
    for pname, pschema in params.items():
        if not hasattr(args, pname):
            continue
        raw = getattr(args, pname)
        if raw is None:
            continue
        kwargs[pname] = coerce_value(raw, pschema)
    return kwargs


async def call_tool_async(tools: dict, tool_name: str, kwargs: dict[str, Any]) -> Any:
    tool = tools[tool_name]
    return await tool.fn(**kwargs)


def cmd_call(tools: dict, tool_name: str, args: argparse.Namespace) -> int:
    if tool_name not in tools:
        print(json.dumps({"error": f"unknown tool: {tool_name!r}"}, ensure_ascii=False))
        return 1
    tool = tools[tool_name]
    try:
        kwargs = collect_tool_kwargs(args, tool)
    except ValueError as e:
        print(json.dumps({"error": f"bad argument: {e}"}, ensure_ascii=False))
        return 1

    try:
        result = asyncio.run(call_tool_async(tools, tool_name, kwargs))
    except Exception as e:  # surface any failure as JSON error
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1

    print(json.dumps(serialize(result), ensure_ascii=False, default=str))
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    # Build the FastMCP app once (registers all tools via decorators at import).
    app = M.create_app()
    tools = app._tool_manager._tools  # plain dict, available synchronously

    parser = build_parser(tools)
    args = parser.parse_args()

    command = args.command
    if command == "list":
        return cmd_list(tools)
    if command == "schema":
        return cmd_schema(tools, args.tool)

    # Otherwise the command IS a tool name.
    return cmd_call(tools, command, args)


if __name__ == "__main__":
    sys.exit(main())

