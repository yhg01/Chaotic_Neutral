"""Personal Agents whose tool loop runs in ZeroClaw, an external assistant program.

This is a trial backend for clocked runs. Each assistant has one ZeroClaw process for the whole
run, with one session in it (`zeroclaw acp`). Each activation is one prompt in that session, so
the program keeps its own history from turn to turn. The process reaches the harness through
one loopback server, the bridge, in two ways:

- its model client posts to an OpenAI-compatible route of the bridge;
- its MCP client lists and calls the assistant's tools on an MCP route of the bridge.

The harness answers both. It makes the real model call, so the campaign budget, the model-call
limit and the clock's think time stay where they are. It runs every tool through the existing
adapters, so the gate, the saved tool events and the request IDs stay where they are too. The
program holds no provider key and no board credential.

Two modes decide what the model reads:

- ``harness``: the model reads the same instructions, history and tool names as on the Deep
  Agents backend. ZeroClaw only decides when to ask again and which tool to run next.
- ``zeroclaw``: the model reads the request as ZeroClaw wrote it, with its own system prompt,
  its own history of the session and its prefixed tool names. Use this mode to measure the
  program, not for evaluation runs.

After a failed attempt the process is stopped. A new process loads the session as it was before
that turn. The saved model messages and tool results of the turn are then given to it again in
order, so no model call and no world effect happens twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

import uvicorn
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain.tools import ToolRuntime
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.tools import ToolException
from langchain_core.utils.function_calling import convert_to_openai_tool
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from agentrpg.agent_surface import NATURAL
from agentrpg.clock import strip_nul
from agentrpg.deep_agent import (
    CLOCKED_SUMMARY_KEEP_TOKENS,
    CLOCKED_SUMMARY_TOKENS,
    AgentRuntimeError,
    AgentTransientError,
    PersonalAgent,
    _ActivationYield,
    _BoundedCalls,
    _digest,
    _unobserved_mail,
    _uuid,
)

HARNESS = "harness"
ZEROCLAW = "zeroclaw"
MODES = (HARNESS, ZEROCLAW)
BINARY_ENV = "AGENTRPG_ZEROCLAW"
SERVER = "home"
PREFIX = SERVER + "__"
ALIAS = "assistant"
PERSONA_FILES = (
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "IDENTITY.md",
    "USER.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
)
UNEXPECTED_CALL = json.dumps(
    {"ok": False, "error": {"code": "not_available", "detail": "That is not available."}}
)
OPEN_CALL = (
    "Tool call {name} with id {call_id} did not complete - no result was recorded. "
    "It may have been cancelled or interrupted."
)
PLACEHOLDER = "Continue."
SUMMARY_LEAD = "Here is a summary of the conversation to date:\n\n"


def _tokens(messages) -> int:
    """About how many tokens these messages take: four characters each."""
    size = 0
    for message in messages:
        content = message.content
        size += len(content if isinstance(content, str) else json.dumps(content, default=str))
        size += len(json.dumps(getattr(message, "tool_calls", None) or [], default=str))
    return size // 4


COMPACT_FILE_CHARS = 6000
PROGRAM_CONTEXT_TOKENS = 1_000_000
NOTE_TOOLS = ("ls", "read_file", "write_file", "edit_file", "glob", "grep")


class _Notes(StateBackend):
    """The private scratch files of one assistant, held outside a graph.

    The parent class gives the file rules and the texts of the library's own file tools. It
    reads and writes a graph channel in two places only, and both are replaced here.
    """

    def __init__(self) -> None:
        super().__init__()
        self.files: dict = {}
        self.changed = False

    def _read_files(self) -> dict:
        return self.files

    def _send_files_update(self, update: dict) -> None:
        for path, data in update.items():
            if data is None:
                self.files.pop(path, None)
            else:
                self.files[path] = data
        self.changed = True


class _BridgeServer(uvicorn.Server):
    """The run's own signal handling stays in place. This server takes no signals."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _bare(name: str) -> str:
    return name.removeprefix(PREFIX)


def _from_wire(items: list[dict]) -> list:
    """Chat-completions messages, as the program wrote them, in the form our models take."""
    messages = []
    for item in items:
        role, content = item.get("role"), item.get("content") or ""
        if role == "system":
            messages.append(SystemMessage(content=content))
        elif role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            calls = [
                {
                    "name": call["function"]["name"],
                    "args": json.loads(call["function"].get("arguments") or "{}"),
                    "id": call["id"],
                    "type": "tool_call",
                }
                for call in item.get("tool_calls") or []
            ]
            messages.append(AIMessage(content=content, tool_calls=calls))
        elif role == "tool":
            messages.append(
                ToolMessage(
                    content=content, tool_call_id=item.get("tool_call_id"), name=item.get("name")
                )
            )
        else:
            raise AgentRuntimeError("The assistant program sent an unknown message role.")
    return messages


@dataclass(eq=False)
class _Ask:
    """One thing the program asks the harness: a model step or a tool call."""

    kind: str
    body: dict
    answer: asyncio.Future


class _Program:
    """One assistant's ZeroClaw process. It lives for the whole run and holds one session."""

    def __init__(self, binary: str, directory: Path) -> None:
        self.binary = binary
        self.directory = directory
        self.process = None
        self.session_id: str | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._number = 0

    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        """Start the process and open its session, or load the session it had before."""
        log = await asyncio.to_thread(open, self.directory / "program.log", "ab")
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.binary,
                "acp",
                "--agent",
                ALIAS,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env={
                    "HOME": str(self.directory / "home"),
                    "PATH": "/usr/bin:/bin",
                    "ZEROCLAW_CONFIG_DIR": str(self.directory / "config"),
                    "TZ": "UTC",
                },
                cwd=str(self.directory),
                start_new_session=True,
                limit=32 * 1024 * 1024,
            )
        finally:
            log.close()
        self._reader = asyncio.create_task(self._read(self.process))
        await self.call("initialize", wait=30)
        saved = self.directory / "session-id"
        known = await asyncio.to_thread(
            lambda: saved.read_text().strip() if saved.is_file() else ""
        )
        if known:
            reply = await self.call("session/load", {"sessionId": known}, wait=30)
            if "error" not in reply:
                self.session_id = known
                return
        reply = await self.call("session/new", {"agentAlias": ALIAS}, wait=30)
        self.session_id = reply["result"]["sessionId"]
        await asyncio.to_thread(saved.write_text, self.session_id)

    async def call(self, method: str, params: dict | None = None, *, wait=None) -> dict:
        """One JSON-RPC request on the program's input, and its answer. wait is in seconds."""
        self._number += 1
        number = self._number
        request = {"jsonrpc": "2.0", "id": number, "method": method}
        if params is not None:
            request["params"] = params
        answer = asyncio.get_running_loop().create_future()
        self._pending[number] = answer
        try:
            self.process.stdin.write((json.dumps(request) + "\n").encode())
            await self.process.stdin.drain()
            async with asyncio.timeout(wait):
                return await answer
        finally:
            self._pending.pop(number, None)

    async def _read(self, process) -> None:
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                answer = None if "method" in message else self._pending.get(message.get("id"))
                if answer is not None and not answer.done():
                    answer.set_result(message)
        finally:
            for answer in self._pending.values():
                if not answer.done():
                    answer.set_exception(ConnectionError("The assistant program closed."))

    async def stop(self) -> None:
        process, self.process = self.process, None
        reader, self._reader = self._reader, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


@dataclass(eq=False)
class _Seat:
    """One Agent's place at the bridge: its secret path, its folder and its open questions."""

    token: str
    directory: Path
    tools: list[dict]
    program: _Program
    asks: asyncio.Queue | None = None

    def open(self) -> asyncio.Queue:
        self.asks = asyncio.Queue()
        return self.asks

    def close(self) -> None:
        asks, self.asks = self.asks, None
        while asks is not None and not asks.empty():
            asks.get_nowait().answer.cancel()


@dataclass(frozen=True)
class _ModelRequest:
    """What the model-call middleware reads of a request. It is not a LangGraph object."""

    state: dict
    messages: list
    tools: list
    system_message: SystemMessage
    tool_choice: object = None

    def override(self, **changes) -> _ModelRequest:
        return replace(self, **changes)


class ZeroClawBridge:
    """The loopback server that ZeroClaw processes of one run talk to."""

    def __init__(
        self,
        binary: str | os.PathLike | None = None,
        *,
        keep: bool = False,
        root: str | os.PathLike | None = None,
    ) -> None:
        binary = binary or os.environ.get(BINARY_ENV)
        if not binary or not Path(binary).is_file():
            raise ValueError(f"Set {BINARY_ENV} to the ZeroClaw program.")
        self.binary = str(binary)
        self.keep = keep or root is not None
        self._given_root = Path(root) if root is not None else None
        self.root: Path | None = None
        self.port: int | None = None
        self._seats: dict[str, _Seat] = {}
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task | None = None

    async def __aenter__(self) -> ZeroClawBridge:
        if self._given_root is not None:
            self._given_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root = self._given_root
        else:
            self.root = Path(tempfile.mkdtemp(prefix="agentrpg-zeroclaw-"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        self._server = _BridgeServer(
            uvicorn.Config(
                self._app(),
                log_level="warning",
                timeout_keep_alive=3600,
                timeout_graceful_shutdown=1,
            )
        )
        self._serving = asyncio.create_task(self._server.serve(sockets=[listener]))
        while not self._server.started:  # noqa: ASYNC110
            if self._serving.done():
                raise AgentRuntimeError("The assistant bridge could not start.")
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *args) -> None:
        for seat in self._seats.values():
            seat.close()
            await seat.program.stop()
        if self._server is not None:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await self._serving
        if self.root is not None and not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    def alive(self) -> bool:
        return self._serving is not None and not self._serving.done()

    def seat(
        self, *, key: str, tools: list[dict], instructions: str, mode: str, max_steps: int
    ) -> _Seat:
        """Give one Agent a secret path and a private ZeroClaw folder. key names the folder."""
        if self.root is None:
            raise AgentRuntimeError("Open the assistant bridge before creating an Agent.")
        token = secrets.token_urlsafe(24)
        directory = self.root / f"seat-{key}"
        workspace = directory / "config" / "agents" / ALIAS / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (directory / "home").mkdir(exist_ok=True)
        for name in PERSONA_FILES:
            # Never write an empty file: ZeroClaw fills it with its own sample persona.
            text = instructions if (name == "AGENTS.md" and mode == ZEROCLAW) else "-\n"
            (workspace / name).write_text(text, encoding="utf-8")
        base = f"http://127.0.0.1:{self.port}/zc/{token}"
        names = [PREFIX + tool["function"]["name"] for tool in tools]
        compact = mode != ZEROCLAW or len(instructions) <= COMPACT_FILE_CHARS
        (directory / "config" / "config.toml").write_text(
            _config(base, names, max_steps, compact=compact), encoding="utf-8"
        )
        seat = _Seat(
            token=token,
            directory=directory,
            tools=tools,
            program=_Program(self.binary, directory),
        )
        self._seats[token] = seat
        return seat

    async def _ask(self, token: str, kind: str, body: dict):
        seat = self._seats.get(token)
        if seat is None or seat.asks is None:
            return None
        ask = _Ask(kind, body, asyncio.get_running_loop().create_future())
        seat.asks.put_nowait(ask)
        try:
            return await ask.answer
        except asyncio.CancelledError:
            return None

    def _app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        unavailable = JSONResponse({"error": {"message": "unavailable"}}, status_code=503)

        @app.get("/zc/{token}/v1/models")
        async def models(token: str):
            return {"object": "list", "data": [{"id": "assistant-model", "object": "model"}]}

        @app.post("/zc/{token}/v1/chat/completions")
        async def chat(token: str, request: Request):
            body = await request.json()
            answer = await self._ask(token, "model", body)
            if answer is None:
                return unavailable
            if not body.get("stream"):
                return answer
            return StreamingResponse(_stream(answer), media_type="text/event-stream")

        @app.get("/zc/{token}/mcp")
        async def stream(token: str):
            return Response(status_code=405)

        @app.delete("/zc/{token}/mcp")
        async def close(token: str):
            return Response(status_code=204)

        @app.post("/zc/{token}/mcp")
        async def mcp(token: str, request: Request):
            body = await request.json()
            seat = self._seats.get(token)
            method, request_id = body.get("method"), body.get("id")
            if seat is None:
                return unavailable
            if request_id is None:
                return Response(status_code=202)
            headers = None
            if method == "initialize":
                headers = {"Mcp-Session-Id": uuid4().hex}
                result = {
                    "protocolVersion": body.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER, "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": tool["function"]["name"],
                            "description": tool["function"]["description"],
                            "inputSchema": tool["function"]["parameters"],
                        }
                        for tool in seat.tools
                    ]
                }
            elif method == "tools/call":
                text = await self._ask(token, "tool", body.get("params") or {})
                if text is None:
                    return unavailable
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                error = {"code": -32601, "message": "Method not found"}
                return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": error})
            return JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": result}, headers=headers
            )

        return app


def _stream(answer: dict):
    """One finished chat-completions reply as a short event stream."""
    choice = answer["choices"][0]
    reply = choice["message"]
    delta = {"role": "assistant", "content": reply.get("content") or ""}
    if reply.get("tool_calls"):
        delta["tool_calls"] = [
            {"index": index, **call} for index, call in enumerate(reply["tool_calls"])
        ]
    base = {
        "id": answer["id"],
        "object": "chat.completion.chunk",
        "created": 0,
        "model": answer["model"],
    }
    first = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    last = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
        "usage": answer["usage"],
    }
    yield f"data: {json.dumps(first)}\n\n"
    yield f"data: {json.dumps(last)}\n\n"
    yield "data: [DONE]\n\n"


def _config(base: str, tool_names: list[str], max_steps: int, *, compact: bool = True) -> str:
    """A ZeroClaw 0.8 configuration with no host tools, no memory and no background work."""
    listed = ", ".join(json.dumps(name) for name in tool_names)
    compact_line = "" if compact else "compact_context = false\n"
    return f"""schema_version = 3

[providers.models.custom.local]
uri = "{base}/v1"
model = "assistant-model"
api_key = "local-bridge"
max_tokens = 4000
context_window = {PROGRAM_CONTEXT_TOKENS}
native_tools = true
timeout_secs = 3600

[agents.{ALIAS}]
model_provider = "custom.local"
risk_profile = "closed"
runtime_profile = "closed"
mcp_bundles = ["harness"]
acp_enable_mcp = true

[agents.{ALIAS}.memory]
backend = "none"

[risk_profiles.closed]
level = "supervised"
allowed_tools = [{json.dumps(tool_names[0])}]
auto_approve = ["*"]
always_ask = []
allowed_commands = []

[runtime_profiles.closed]
max_tool_iterations = {max_steps}
parallel_tools = false
{compact_line}max_actions_per_hour = 1000000
max_cost_per_day_cents = 100000000
tool_call_dedup_exempt = [{listed}]

[pacing]
loop_detection_enabled = false

[acp]
session_timeout_secs = 31536000

[reliability]
provider_retries = 0

[tools]
optional = []

[mcp]
enabled = true
deferred_loading = false

[[mcp.servers]]
name = "{SERVER}"
transport = "http"
url = "{base}/mcp"
tool_timeout_secs = 600

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
"""


def _json(message) -> Jsonb:
    """A message as PostgreSQL can hold it: no NUL, and no value that JSON cannot write."""
    return Jsonb(
        strip_nul(message_to_dict(message)), dumps=lambda value: json.dumps(value, default=str)
    )


@dataclass
class ZeroClawStats:
    """What one Agent's program did, for reports. Counts only; no message text."""

    processes: int = 0
    turns: int = 0
    model_asks: int = 0
    model_calls: int = 0
    replayed_messages: int = 0
    tool_asks: int = 0
    replayed_results: int = 0
    unexpected_calls: int = 0
    changed_arguments: int = 0
    unanswered_calls: int = 0
    open_calls_closed: int = 0
    summaries: int = 0
    early_exits: int = 0


class ZeroClawAgent(PersonalAgent):
    """A clocked personal Agent whose loop runs in one long-lived ZeroClaw process.

    The parent class still gives the session lock, the task receipts, the identity check, the
    tool adapters and the saved tool events. Its graph is built and not used.
    """

    def __init__(self, *, bridge: ZeroClawBridge, mode: str = HARNESS, **arguments) -> None:
        if mode not in MODES:
            raise ValueError("mode must be harness or zeroclaw.")
        super().__init__(**arguments)
        if self.gate is None or self.surface != NATURAL:
            raise ValueError("The ZeroClaw backend is for clocked runs on the natural surface.")
        self.bridge = bridge
        self.mode = mode
        self.stats = ZeroClawStats()
        self._instructions_hash = _digest(f"{self._instructions_hash}:zeroclaw:{mode}")
        self._offered = {tool.name: tool for tool in self._tools()}
        self._files = _Notes()
        self._file_tools = {
            tool.name: tool
            for tool in FilesystemMiddleware(backend=self._files, tools=list(NOTE_TOOLS)).tools
        }
        self._offered.update(self._file_tools)
        self._specs = [convert_to_openai_tool(tool) for tool in self._offered.values()]
        self._decide = _ActivationYield(self)._decide
        self._calls = _BoundedCalls(self)
        self._seat = bridge.seat(
            key=str(self.agent_id),
            tools=self._specs,
            instructions=self.instructions,
            mode=mode,
            max_steps=self.max_model_calls + 8,
        )
        self._transcript: list = []
        self._start = 0
        self._delivered = 0
        self._arrived: set[int] = set()
        self._result: dict | None = None
        self._notified: list[str] = []
        # Saved history keeps every message after a summary: the clock reads stamps from all.
        self._summary: tuple[int, str] | None = None
        self.summary_tokens = CLOCKED_SUMMARY_TOKENS
        self.summary_keep_tokens = CLOCKED_SUMMARY_KEEP_TOKENS

    async def activation_messages(self) -> list:
        return await self._load()

    async def _load(self) -> list:
        async with self.runtime.pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT message FROM zeroclaw_messages WHERE session_key=%s ORDER BY seq",
                    (self.session_key,),
                )
            ).fetchall()
        return messages_from_dict([row["message"] for row in rows])

    async def _append(self, message, *, note: dict | None = None) -> None:
        async with self.runtime.pool.connection() as connection:
            await connection.execute(
                """INSERT INTO zeroclaw_messages(session_key, seq, task_id, message, note)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    self.session_key,
                    len(self._transcript),
                    self._active_task_id,
                    _json(message),
                    Jsonb(note or {}),
                ),
            )
        self._transcript.append(message)

    async def _replace(self, index: int, message) -> None:
        async with self.runtime.pool.connection() as connection:
            await connection.execute(
                "UPDATE zeroclaw_messages SET message=%s WHERE session_key=%s AND seq=%s",
                (_json(message), self.session_key, index),
            )
        self._transcript[index] = message

    async def _load_files(self) -> dict:
        async with self.runtime.pool.connection() as connection:
            row = await (
                await connection.execute(
                    "SELECT files FROM zeroclaw_files WHERE session_key=%s", (self.session_key,)
                )
            ).fetchone()
        return dict(row["files"]) if row else {}

    async def _save_files(self) -> None:
        async with self.runtime.pool.connection() as connection:
            await connection.execute(
                """INSERT INTO zeroclaw_files(session_key, files) VALUES (%s, %s)
                   ON CONFLICT (session_key) DO UPDATE
                   SET files = EXCLUDED.files, updated_at = clock_timestamp()""",
                (self.session_key, Jsonb(strip_nul(self._files.files))),
            )
        self._files.changed = False

    async def _file_call(self, tool, tool_call: dict) -> ToolMessage:
        """Run one private file tool. The library gives these tools their state at call time."""
        runtime = ToolRuntime(
            state={"messages": self._transcript, "files": self._files.files},
            context=None,
            config={},
            stream_writer=lambda _chunk: None,
            tool_call_id=tool_call["id"],
            store=None,
        )
        try:
            result = await tool.ainvoke(
                {**tool_call, "args": {**tool_call["args"], "runtime": runtime}}
            )
        except (ValidationError, ToolException) as error:
            result = ToolMessage(
                content=f"Error: {error}",
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
                status="error",
            )
        if self._files.changed:
            await self._save_files()
        if not isinstance(result, ToolMessage):
            result = ToolMessage(
                content=str(result), name=tool_call["name"], tool_call_id=tool_call["id"]
            )
        return result

    async def _notes(self) -> list[dict]:
        async with self.runtime.pool.connection() as connection:
            rows = await (
                await connection.execute(
                    "SELECT seq, task_id, note FROM zeroclaw_messages WHERE session_key=%s "
                    "AND note <> '{}'::jsonb ORDER BY seq",
                    (self.session_key,),
                )
            ).fetchall()
        return rows

    async def _run_locked(self, task: str, task_id: UUID) -> str:
        receipt = await self._claim_task(task, task_id)
        if receipt["status"] == "completed":
            return receipt["result"]
        namespace = uuid5(self._session_namespace, str(task_id))
        self.board.operation_namespace = namespace
        self.environment.operation_namespace = namespace
        if self.web is not None:
            self.web.operation_namespace = namespace
        self._transcript = await self._load()
        self._files.files = await self._load_files()
        notes = await self._notes()
        told = [row for row in notes if "notified_mail_ids" in row["note"]]
        marks = [row for row in notes if "summary" in row["note"]]
        self._summary = (marks[-1]["seq"], marks[-1]["note"]["summary"]) if marks else None
        start = next(
            (
                index
                for index, message in enumerate(self._transcript)
                if message.type == "human" and message.id == str(task_id)
            ),
            None,
        )
        if start is None:
            await self._close_open_calls()
            earlier = told[-1]["note"]["notified_mail_ids"] if told else []
            self._notified = _unobserved_mail(self._activation_envelope, self._transcript, earlier)
            start = len(self._transcript)
            await self._append(
                HumanMessage(content=task, id=str(task_id)),
                note={"notified_mail_ids": self._notified},
            )
        else:
            saved = next((row["note"] for row in told if row["task_id"] == task_id), {})
            self._notified = saved.get("notified_mail_ids", [])
        self._start = start
        self._result = None
        await self._drive(task)
        last = self._transcript[-1]
        if self._result is None and (not isinstance(last, AIMessage) or last.tool_calls):
            raise AgentRuntimeError(
                "The Agent did not finish its response. Retry the same task ID."
            )
        answer = (
            self._result_model.model_validate(self._result)
            if self._result is not None
            else self._result_model(status="responded", text=last.text)
        ).model_dump_json()
        self.messages = list(self._transcript)
        async with self.runtime.pool.connection() as connection:
            await connection.execute(
                """UPDATE agent_tasks SET status = 'completed', result = %s,
                   completed_at = clock_timestamp() WHERE session_key = %s AND task_id = %s""",
                (answer, self.session_key, task_id),
            )
        return answer

    def _visible(self) -> list:
        """The history as the model reads it: the last summary, then the messages after it."""
        if self._summary is None:
            return list(self._transcript)
        position, text = self._summary
        lead = HumanMessage(
            content=SUMMARY_LEAD + text, additional_kwargs={"lc_source": "summarization"}
        )
        return [lead, *self._transcript[position:]]

    async def _summarize_if_long(self) -> None:
        """Put a summary in place of older history when the model's input is too long.

        This counts as one model call of the task, as on the Deep Agents backend.
        """
        shown = self._visible()
        if _tokens(shown) <= self.summary_tokens:
            return
        first = self._summary[0] if self._summary else 0
        kept, cut = 0, None
        for index in range(len(self._transcript) - 1, first, -1):
            kept += _tokens([self._transcript[index]])
            if kept >= self.summary_keep_tokens:
                cut = index
                break
        if cut is None:
            return
        while cut < len(self._transcript) and self._transcript[cut].type == "tool":
            cut += 1
        if cut >= len(self._transcript) or cut <= first:
            return
        older = shown[: len(shown) - (len(self._transcript) - cut)]
        self.gate.before_model(self.clock_process, self._transcript)
        text = await self._summarize(older)
        async with self.runtime.pool.connection() as connection:
            await connection.execute(
                "UPDATE zeroclaw_messages SET note = note || %s WHERE session_key=%s AND seq=%s",
                (Jsonb({"summary": strip_nul(text)}), self.session_key, cut),
            )
        self._summary = (cut, text)
        self.stats.summaries += 1

    async def _close_open_calls(self) -> None:
        """Give a result to every call that an abandoned activation left open.

        A provider refuses a history in which a model message has a call with no result. One
        abandoned activation would then stop every later one.
        """
        last = next(
            (
                i
                for i in range(len(self._transcript) - 1, -1, -1)
                if self._transcript[i].type == "ai"
            ),
            None,
        )
        if last is None:
            return
        answered = {
            message.tool_call_id
            for message in self._transcript[last + 1 :]
            if message.type == "tool"
        }
        for call in self._transcript[last].tool_calls:
            if call["id"] in answered:
                continue
            self.stats.open_calls_closed += 1
            await self._append(
                ToolMessage(
                    content=OPEN_CALL.format(name=call["name"], call_id=call["id"]),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                    id=str(uuid4()),
                )
            )

    async def _drive(self, task: str) -> None:
        """Give the program one prompt in its session, and answer what it asks until it ends."""
        self._delivered = 0
        self._arrived = set()
        program = self._seat.program
        asks = self._seat.open()
        asking = waiting = reply = None
        asked = False
        try:
            if not program.alive():
                await program.start()
                self.stats.processes += 1
            self.stats.turns += 1
            waiting = asyncio.create_task(
                program.call(
                    "session/prompt",
                    {
                        "sessionId": program.session_id,
                        "prompt": PLACEHOLDER if self.mode == HARNESS else task,
                    },
                )
            )
            waiting.add_done_callback(lambda task: task.cancelled() or task.exception())
            while True:
                asking = asyncio.create_task(asks.get())
                done, _ = await asyncio.wait(
                    {asking, waiting},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=self.timeout_seconds,
                )
                if not done:
                    raise AgentTransientError(tool=True)
                if asking not in done:
                    break
                ask, asking, asked = asking.result(), None, True
                try:
                    answer = await (
                        self._model_step(ask.body)
                        if ask.kind == "model"
                        else self._tool_step(ask.body)
                    )
                except BaseException:
                    ask.answer.cancel()
                    raise
                if not ask.answer.done():
                    ask.answer.set_result(answer)
            reply = waiting.result()
        except (ConnectionError, OSError, TimeoutError):
            reply = None
        except BaseException:
            await program.stop()
            raise
        finally:
            if asking is not None:
                if asking.done() and not asking.cancelled() and asking.exception() is None:
                    asking.result().answer.cancel()
                asking.cancel()
            if waiting is not None and not waiting.done():
                waiting.cancel()
            self._seat.close()
        if reply is None or "error" in reply:
            self.stats.early_exits += 1
            # The stop reason can hold model text. Keep it out of errors, reports and prompts.
            await asyncio.to_thread(self._note_stop, reply)
            await program.stop()
            if not asked or not self.bridge.alive():
                raise AgentTransientError(tool=True)
            if self._result is None:
                raise AgentRuntimeError(
                    "The assistant program stopped early. Retry the same task ID."
                )

    def _note_stop(self, reply: dict | None) -> None:
        line = json.dumps(
            {
                "task_id": str(self._active_task_id),
                "delivered": self._delivered,
                "error": None if reply is None else reply.get("error"),
            },
            default=str,
        )
        with (self._seat.directory / "stops.jsonl").open("a", encoding="utf-8") as file:
            file.write(line[:4000] + "\n")

    def _state(self) -> dict:
        return {
            "activation_mode": True,
            "messages": self._transcript,
            "activation_envelope": self._activation_envelope,
            "notified_mail_ids": self._notified,
        }

    def _said(self) -> list[int]:
        """Positions of this activation's model messages."""
        return [
            index
            for index in range(self._start, len(self._transcript))
            if self._transcript[index].type == "ai"
        ]

    async def _settle(self, body: dict) -> None:
        """Before another model step, every call of the last model message needs one result.

        The program can leave a call out, for example when it refuses a name. In zeroclaw mode
        its own words for that call then stand as the result. In harness mode a fixed sentence
        of ours does.
        """
        said = self._said()
        if not self._delivered or self._delivered > len(said):
            return
        position = said[self._delivered - 1]
        answered = {
            message.tool_call_id
            for message in self._transcript[position + 1 :]
            if message.type == "tool"
        }
        reported = {
            item.get("tool_call_id"): item.get("content")
            for item in body.get("messages", [])
            if item.get("role") == "tool"
        }
        for call in self._transcript[position].tool_calls:
            if call["id"] in answered:
                continue
            self.stats.unanswered_calls += 1
            content = reported.get(call["id"]) if self.mode == ZEROCLAW else None
            await self._append(
                ToolMessage(
                    content=content if isinstance(content, str) else UNEXPECTED_CALL,
                    name=call["name"],
                    tool_call_id=call["id"],
                    id=str(uuid4()),
                )
            )

    async def _model_step(self, body: dict) -> dict:
        self.stats.model_asks += 1
        heard = 0
        for item in body.get("messages", []):
            if item.get("role") == "user":
                heard = 0
            elif item.get("role") == "assistant":
                heard += 1
        if heard != self._delivered:
            raise AgentRuntimeError("The assistant program is out of step. Retry the same task ID.")
        await self._settle(body)
        said = self._said()
        if self._delivered < len(said):
            message = self._transcript[said[self._delivered]]
            self._delivered += 1
            self._arrived = set()
            self.stats.replayed_messages += 1
            return self._wire(message, body)
        decision = await self._decide(self._state())
        for changed in (decision or {}).get("messages", []):
            index = next(
                index
                for index in range(len(self._transcript) - 1, self._start, -1)
                if getattr(self._transcript[index], "tool_call_id", None) == changed.tool_call_id
            )
            await self._replace(index, changed)
        if decision and decision.get("activation_result") is not None:
            self._result = decision["activation_result"]
        if self._result is not None:
            # Not model text: this reply only lets the program stop. Never save it as history.
            return self._wire(AIMessage(content=self._result.get("text") or "Done."), body)
        if self.mode == HARNESS:
            await self._summarize_if_long()
        base = self._visible() if self.mode == HARNESS else list(self._transcript)

        async def handler(request: _ModelRequest):
            if self.mode == HARNESS:
                messages = [request.system_message, *request.messages]
                tools = request.tools
            else:
                messages = [*_from_wire(body["messages"]), *request.messages[len(base) :]]
                tools = body.get("tools") or []
            message = await self.model.bind_tools(tools).ainvoke(
                messages, {"callbacks": self.config["callbacks"]}
            )
            if message.tool_calls:
                message = message.model_copy(
                    update={
                        "tool_calls": [
                            {**call, "name": _bare(call["name"])} for call in message.tool_calls
                        ]
                    }
                )
            if message.id is None:
                message = message.model_copy(update={"id": str(uuid4())})
            return SimpleNamespace(result=[message])

        response = await self._calls.awrap_model_call(
            _ModelRequest(
                state=self._state(),
                messages=base,
                tools=self._specs,
                system_message=SystemMessage(content=self.instructions),
            ),
            handler,
        )
        message = response.result[-1]
        self.stats.model_calls += 1
        await self._append(message)
        self._delivered += 1
        self._arrived = set()
        return self._wire(message, body)

    def _wire(self, message: AIMessage, body: dict) -> dict:
        """One model message in the chat-completions form that the program reads."""
        calls = [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": PREFIX + call["name"], "arguments": json.dumps(call["args"])},
            }
            for call in message.tool_calls
        ]
        reply = {"role": "assistant", "content": message.text or None}
        if calls:
            reply["tool_calls"] = calls
        return {
            "id": "gen-" + uuid4().hex,
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model") or "assistant-model",
            "choices": [
                {"index": 0, "message": reply, "finish_reason": "tool_calls" if calls else "stop"}
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _tool_step(self, body: dict) -> str:
        """Run the call that the program has reached, in the order the model gave."""
        self.stats.tool_asks += 1
        name, arguments = _bare(str(body.get("name") or "")), body.get("arguments") or {}
        said = self._said()
        if not self._delivered or self._delivered > len(said):
            self.stats.unexpected_calls += 1
            return UNEXPECTED_CALL
        position = said[self._delivered - 1]
        # MCP carries no call ID: match by name and arguments in the model's order, else by name.
        waiting = [
            (index, call)
            for index, call in enumerate(self._transcript[position].tool_calls)
            if call["name"] == name and index not in self._arrived
        ]
        found = next(
            (item for item in waiting if item[1]["args"] == arguments),
            waiting[0] if waiting else None,
        )
        if found is None:
            self.stats.unexpected_calls += 1
            return UNEXPECTED_CALL
        index, call = found
        self._arrived.add(index)
        if call["args"] != arguments:
            self.stats.changed_arguments += 1
        saved = next(
            (
                message
                for message in self._transcript[position + 1 :]
                if message.type == "tool" and message.tool_call_id == call["id"]
            ),
            None,
        )
        if saved is not None:
            self.stats.replayed_results += 1
            return saved.content
        tool = self._offered.get(name)
        if tool is None:
            self.stats.unexpected_calls += 1
            result = ToolMessage(content=UNEXPECTED_CALL, name=name, tool_call_id=call["id"])
        else:
            # Run the model's saved arguments, never the program's copy of them.
            tool_call = {"name": name, "args": call["args"], "id": call["id"], "type": "tool_call"}

            async def run(_request):
                if name in self._file_tools:
                    return await self._file_call(tool, tool_call)
                return await tool.ainvoke(tool_call)

            result = await self._calls._gated_tool_call(
                SimpleNamespace(tool_call=tool_call, state={"messages": self._transcript}),
                run,
                self.gate,
            )
        if result.id is None:
            result = result.model_copy(update={"id": str(uuid4())})
        await self._append(result)
        return result.content if isinstance(result.content, str) else json.dumps(result.content)


def zeroclaw_factory(bridge: ZeroClawBridge, *, mode: str = HARNESS):
    """An agent factory with the arguments of AgentRuntime.agent, for clocked runs."""

    def make(runtime, *, agent_id, session_id, callbacks=(), **arguments) -> ZeroClawAgent:
        if not runtime._open:
            raise AgentRuntimeError("Open the Agent runtime before creating an Agent.")
        return ZeroClawAgent(
            runtime=runtime,
            bridge=bridge,
            mode=mode,
            agent_id=_uuid(agent_id),
            session_id=_uuid(session_id),
            callbacks=callbacks,
            **arguments,
        )

    return make
