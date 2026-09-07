#!/usr/bin/env python3
"""Stdio MCP handshake test for the local yargi-mcp server.

Sends initialize -> notifications/initialized -> tools/list over stdio and
prints the server's JSON-RPC responses. Verifies Antigravity will be able to
talk to the server spawned by:  uv run --project <dir> yargi-mcp
"""
import json
import subprocess
import sys
import time

PROJ = r"C:\dev\mcp\yargi-mcp"


def rpc(method, params=None, id_=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return json.dumps(msg, ensure_ascii=False)


def main() -> int:
    proc = subprocess.Popen(
        ["uv", "run", "--project", PROJ, "yargi-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # discard module INFO logs (cp857) entirely
        bufsize=1,
    )

    requests = [
        rpc("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "cline-test", "version": "1.0"}}, id_=1),
        rpc("notifications/initialized"),
        rpc("tools/list", {}, id_=2),
    ]
    payload = "\n".join(requests) + "\n"
    try:
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass

    # Read stdout lines for up to ~45s, stop after we get the tools/list reply (id=2).
    deadline = time.time() + 45
    got_ids = set()
    out_lines = []
    while time.time() < deadline:
        raw = proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        out_lines.append(line)
        try:
            obj = json.loads(line)
            if "id" in obj:
                got_ids.add(obj["id"])
        except Exception:
            pass
        if 1 in got_ids and 2 in got_ids:
            break

    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    print("=== STDOUT (MCP JSON-RPC responses) ===")
    for ln in out_lines:
        print(ln)
    print(f"\n=== got response ids: {sorted(got_ids)} ===")
    print("=== STDERR discarded (module INFO logs) ===")

    return 0 if (1 in got_ids and 2 in got_ids) else 1


if __name__ == "__main__":
    sys.exit(main())
