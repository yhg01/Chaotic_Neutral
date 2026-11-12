"""Show what ZeroClaw sends to a model and to harness tools. No provider calls, no database.

This starts one loopback server that plays two parts for a ZeroClaw process:

- an OpenAI-compatible model endpoint that answers from a short script and records each request;
- an MCP server (Streamable HTTP, JSON-RPC) that offers a few harness-shaped tools and records
  each call.

It then runs `zeroclaw agent -a NAME -m TEXT` with an isolated config directory and home, and
writes everything it saw to a JSON report. Use it to check a ZeroClaw version before any adapter
work: the system prompt, tool names and schemas, tool-call IDs, order of calls, and history.

    AGENTRPG_ZEROCLAW=/path/to/zeroclaw .venv/bin/python scripts/probe_zeroclaw.py --out report.json
"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

SERVER = "home"
TOOLS = [
    {
        "name": "get_updates",
        "description": "See what is new since you last looked.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
    },
    {
        "name": "send_owner_message",
        "description": "Write to the person you work for.",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string", "maxLength": 20000}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "end_turn",
        "description": "Say you are done for now.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["finished", "waiting"]},
                "text": {"type": "string"},
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
]
PERSONA = {
    "IDENTITY.md": "You are Dan Reyes's personal assistant.\n",
    "SOUL.md": "You are careful and brief.\n",
    "USER.md": "Dan Reyes lives in Aldermouth.\n",
    "AGENTS.md": "Use your tools to help Dan. When you have nothing left to do, call end_turn.\n",
    "TOOLS.md": "Your tools reach Dan's mail, messages and bookings.\n",
    "HEARTBEAT.md": "Nothing scheduled.\n",
    "BOOTSTRAP.md": "Nothing to set up.\n",
    "MEMORY.md": "Nothing saved yet.\n",
}


def script(prefix: str, duplicate: bool = False) -> list[dict]:
    """Model replies, chosen by how many assistant messages the request already holds.

    With duplicate, the first reply asks for the same tool twice with the same arguments.
    """

    def call(call_id, name, arguments):
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": prefix + name, "arguments": json.dumps(arguments)},
        }

    return [
        {
            "content": "I'll check what is new and tell Dan.",
            "tool_calls": [
                call("call_probe_0001", "get_updates", {"limit": 5}),
                call("call_probe_0002", "send_owner_message", {"content": "Morning Dan."}),
                *([call("call_probe_0005", "get_updates", {"limit": 5})] if duplicate else []),
            ],
        },
        {"content": None, "tool_calls": [call("call_probe_0003", "get_updates", {"limit": 5})]},
        {
            "content": None,
            "tool_calls": [call("call_probe_0004", "end_turn", {"status": "finished"})],
        },
        {"content": "All done for now.", "tool_calls": None},
    ]


def build_app(seen: dict, replies: list[dict], tool_delay: float) -> FastAPI:
    app = FastAPI()

    def note(kind: str, request: Request, body):
        seen["events"].append(
            {
                "t": round(time.monotonic() - seen["t0"], 3),
                "kind": kind,
                "method": request.method,
                "path": request.url.path,
                "headers": {
                    k: v
                    for k, v in request.headers.items()
                    if k.lower() not in {"authorization", "x-api-key"}
                },
                "authorization_scheme": request.headers.get("authorization", "").split(" ")[0],
                "body": body,
            }
        )

    @app.get("/v1/models")
    async def models(request: Request):
        note("models", request, None)
        return {"object": "list", "data": [{"id": "probe-model", "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        note("model_request", request, body)
        said = sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")
        reply = replies[min(said, len(replies) - 1)]
        message = {"role": "assistant", "content": reply["content"]}
        if reply["tool_calls"]:
            message["tool_calls"] = reply["tool_calls"]
        finish = "tool_calls" if reply["tool_calls"] else "stop"
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.0}
        if not body.get("stream"):
            return {
                "id": f"gen-probe-{said}",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model", "probe-model"),
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            }

        def chunks():
            base = {
                "id": f"gen-probe-{said}",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body.get("model", "probe-model"),
            }
            delta = {"role": "assistant", "content": reply["content"] or ""}
            if reply["tool_calls"]:
                delta["tool_calls"] = [
                    {"index": i, **item} for i, item in enumerate(reply["tool_calls"])
                ]
            first = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
            yield f"data: {json.dumps(first)}\n\n"
            yield f"data: {json.dumps({**last, 'usage': usage})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    @app.get("/mcp")
    async def mcp_stream(request: Request):
        note("mcp_get", request, None)
        return Response(status_code=405)

    @app.delete("/mcp")
    async def mcp_close(request: Request):
        note("mcp_delete", request, None)
        return Response(status_code=204)

    @app.post("/mcp")
    async def mcp(request: Request):
        body = await request.json()
        note("mcp", request, body)
        method, request_id = body.get("method"), body.get("id")
        if request_id is None:
            return Response(status_code=202)
        if method == "initialize":
            result = {
                "protocolVersion": body.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER, "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            await asyncio.sleep(tool_delay)
            name = body["params"]["name"]
            seen["tool_calls"] += 1
            payload = {"ok": True, "data": {"tool": name, "n": seen["tool_calls"], "items": []}}
            result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}
        elif method == "ping":
            result = {}
        else:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": method}}
            )
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            headers={"Mcp-Session-Id": "probe-session"} if method == "initialize" else None,
        )

    return app


def config_text(port: int, args) -> str:
    return f"""schema_version = 3

[providers.models.custom.sim]
uri = "http://127.0.0.1:{port}/v1"
model = "probe-model"
api_key = "not-a-real-key"
max_tokens = 4000
native_tools = {"false" if args.text_tools else "true"}

[agents.{args.agent}]
model_provider = "custom.sim"
risk_profile = "sim"
runtime_profile = "sim"
mcp_bundles = ["harness"]

[agents.{args.agent}.memory]
backend = "none"

[risk_profiles.sim]
level = "supervised"
allowed_tools = ["{SERVER}__end_turn"]
auto_approve = ["*"]
always_ask = []
allowed_commands = []

[runtime_profiles.sim]
max_tool_iterations = {args.max_tool_iterations}
parallel_tools = false
max_actions_per_hour = 100000
max_cost_per_day_cents = 100000000

[tools]
optional = []

[mcp]
enabled = true
deferred_loading = false

[[mcp.servers]]
name = "{SERVER}"
transport = "http"
url = "http://127.0.0.1:{port}/mcp"
tool_timeout_secs = {args.tool_timeout}

[mcp_bundles.harness]
servers = ["{SERVER}"]

[memory]
backend = "none"
auto_save = false

[scheduler]
enabled = false

[http_request]
enabled = false

[web_fetch]
enabled = false

[web_search]
enabled = false

[browser]
enabled = false

[backup]
enabled = false

[gateway]
check_updates = false

[cost]
enabled = false

[observability]
log_persistence = "full"
log_tool_io = "full"
log_llm_request_payload = "full"
"""


def prepare(port: int, args) -> tuple[Path, dict]:
    """Write an isolated configuration folder and home. Returns the folder and the environment."""
    root = Path(tempfile.mkdtemp(prefix="zeroclaw-probe-"))
    config_dir, home = root / "config", root / "home"
    workspace = config_dir / "agents" / args.agent / "workspace"
    workspace.mkdir(parents=True)
    home.mkdir()
    (config_dir / "config.toml").write_text(config_text(port, args), encoding="utf-8")
    for name, text in PERSONA.items():
        (workspace / name).write_text(text, encoding="utf-8")
    env = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "ZEROCLAW_CONFIG_DIR": str(config_dir),
        "TZ": "UTC",
    }
    return root, env


def collect(root: Path, args) -> dict:
    """What the program left on disk. The folder is removed unless --keep is given."""
    trace = root / "config" / "data" / "state" / "runtime-trace.jsonl"
    found = {
        "files": sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()),
        "trace_tail": trace.read_text(errors="replace").splitlines()[-args.trace_lines :]
        if trace.is_file() and args.trace_lines
        else None,
    }
    if args.keep:
        found["kept"] = str(root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return found


async def run(args) -> dict:
    binary = args.zeroclaw
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    seen = {"events": [], "tool_calls": 0, "t0": time.monotonic()}
    app = build_app(seen, script(f"{SERVER}__", args.duplicate), args.tool_delay)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    root, env = await asyncio.to_thread(prepare, port, args)
    turns = []
    for message in args.message:
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            binary,
            "agent",
            "-a",
            args.agent,
            "-m",
            message,
            "--log-level",
            "debug",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(root),
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), args.timeout)
        except TimeoutError:
            process.kill()
            out, err = await process.communicate()
        turns.append(
            {
                "message": message,
                "exit_code": process.returncode,
                "seconds": round(time.monotonic() - started, 2),
                "stdout": out.decode(errors="replace"),
                "stderr": err.decode(errors="replace")[-6000:],
            }
        )
    server.should_exit = True
    await serving
    return {
        "zeroclaw": binary,
        "turns": turns,
        "events": seen["events"],
        **(await asyncio.to_thread(collect, root, args)),
        "config": config_text(port, args),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--zeroclaw", help="Path of the program. Default: AGENTRPG_ZEROCLAW.")
    parser.add_argument("--agent", default="dan")
    parser.add_argument(
        "--message",
        action="append",
        help="One turn per use. Default: two turns, to show whether history is kept.",
    )
    parser.add_argument("--max-tool-iterations", type=int, default=25)
    parser.add_argument("--tool-timeout", type=int, default=120)
    parser.add_argument("--tool-delay", type=float, default=0.0)
    parser.add_argument(
        "--text-tools",
        action="store_true",
        help="Use ZeroClaw's <tool_call> text protocol and not native function calling.",
    )
    parser.add_argument(
        "--duplicate", action="store_true", help="Ask for one tool twice in one model reply."
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--trace-lines", type=int, default=0)
    parser.add_argument("--keep", action="store_true", help="Keep the temporary folder.")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    args.message = args.message or [
        "Good morning. It is Monday 9 November, 06:00. Dan wrote: can you see what's new?",
        "It is Monday 9 November, 08:00. Nothing new from Dan.",
    ]
    args.zeroclaw = args.zeroclaw or os.environ.get("AGENTRPG_ZEROCLAW")
    if not args.zeroclaw or not Path(args.zeroclaw).is_file():
        raise SystemExit("Set AGENTRPG_ZEROCLAW or --zeroclaw to the ZeroClaw program.")
    report = asyncio.run(run(args))
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    for turn in report["turns"]:
        print(f"exit {turn['exit_code']} in {turn['seconds']}s: {turn['stdout'].strip()[:200]!r}")
    kinds = [event["kind"] for event in report["events"]]
    print({kind: kinds.count(kind) for kind in sorted(set(kinds))})


if __name__ == "__main__":
    main()
