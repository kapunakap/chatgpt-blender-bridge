#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BLENDER_MCP_HOST="${BLENDER_MCP_HOST:-127.0.0.1}"
BLENDER_MCP_PORT="${BLENDER_MCP_PORT:-9876}"
BLENDER_MCP_COMMAND="${BLENDER_MCP_COMMAND:-}"
BLENDER_MCP_PYTHON="${BLENDER_MCP_PYTHON:-}"

printf 'ChatGPT Blender Bridge - Local MCP Acceptance Gate\n'
printf '===================================================\n\n'

bash scripts/doctor.sh

if [[ -z "$BLENDER_MCP_COMMAND" || ! -x "$BLENDER_MCP_COMMAND" ]]; then
  printf '\nLOCAL MCP GATE: FAIL\n'
  printf 'BLENDER_MCP_COMMAND must point to an executable blender-mcp binary.\n'
  exit 1
fi

if [[ -z "$BLENDER_MCP_PYTHON" || ! -x "$BLENDER_MCP_PYTHON" ]]; then
  printf '\nLOCAL MCP GATE: FAIL\n'
  printf 'BLENDER_MCP_PYTHON must point to the Python interpreter for the Blender MCP environment.\n'
  exit 1
fi

export BLENDER_MCP_HOST BLENDER_MCP_PORT BLENDER_MCP_COMMAND

"$BLENDER_MCP_PYTHON" <<'PY'
import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> None:
    command = os.environ["BLENDER_MCP_COMMAND"]
    host = os.environ["BLENDER_MCP_HOST"]
    port = os.environ["BLENDER_MCP_PORT"]

    child_env = os.environ.copy()
    child_env["BLENDER_MCP_HOST"] = host
    child_env["BLENDER_MCP_PORT"] = port

    params = StdioServerParameters(
        command=command,
        args=["--transport", "stdio"],
        env=child_env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_name = getattr(init.serverInfo, "name", "")
            if server_name != "blender-mcp":
                raise RuntimeError(f"unexpected MCP server name: {server_name!r}")

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = "get_blendfile_summary_datablocks"
            if required not in names:
                raise RuntimeError(f"required Blender read tool missing: {required}")

            response = await session.call_tool(required, {})
            if getattr(response, "isError", False):
                raise RuntimeError("Blender read-only MCP tool returned isError=true")

            structured = getattr(response, "structuredContent", None)
            print(json.dumps({
                "server": server_name,
                "tool_count": len(tools.tools),
                "read_only_tool": required,
                "read_only_tool_ok": True,
                "structured_content": structured is not None,
            }, sort_keys=True))


asyncio.run(main())
PY

printf '\nLOCAL MCP GATE: PASS\n\n'
printf 'This proves the real local stdio MCP server can initialize, discover tools,\n'
printf 'and execute a read-only Blender tool against the running Blender instance.\n'
printf 'It still does NOT prove the cloud/tunnel/plugin path.\n\n'
printf 'Final end-to-end acceptance from ChatGPT must prove:\n'
printf '  [ ] @Blender connects without a 502\n'
printf '  [ ] live scene information is retrieved\n'
printf '  [ ] a harmless execute_blender_code call succeeds through the plugin\n'
printf '  [ ] returned state matches the running Blender instance\n'
printf '  [ ] scene/object state remains unchanged\n'
