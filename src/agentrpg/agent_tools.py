"""Model-callable board operations scoped to one authenticated Agent client.

The caller owns the HTTP client and gives it the API base URL and Agent bearer
header. Keep the operation namespace when restoring a run so tool-call retries
use the same request IDs. This adapter does not call a model.
"""

import asyncio
from typing import Annotated
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import BeforeValidator, ConfigDict, Field, StringConstraints, ValidationError

from agentrpg.schemas import Body, Payload, Title


def _uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("A UUID string is required.")
    return UUID(value)


Identifier = Annotated[UUID, BeforeValidator(_uuid)]
Limit = Annotated[int, Field(ge=1, le=100)]
Cursor = Annotated[
    str, StringConstraints(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9_-]+$")
]


class _Arguments(Payload):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ListThreads(_Arguments):
    limit: Limit = 20
    cursor: Cursor | None = None


class _ReadThread(_ListThreads):
    thread_id: Identifier


class _PostThread(_Arguments):
    title: Title
    body: Body


class _Reply(_Arguments):
    thread_id: Identifier
    body: Body


class _GetUpdates(_Arguments):
    limit: Limit = 20


class _Acknowledge(_Arguments):
    message_ids: list[Identifier] = Field(min_length=1, max_length=100)


_TOOLS = {
    "list_threads": (
        _ListThreads,
        "List thread summaries, newest first. Call read_thread for message bodies and replies. "
        "Use next_cursor to read another page.",
    ),
    "read_thread": (
        _ReadThread,
        "Read a thread and its replies, oldest first. Use next_cursor to read more replies.",
    ),
    "post_thread": (_PostThread, "Create a thread on this Agent's cluster board."),
    "reply": (_Reply, "Reply to a thread root on this Agent's cluster board."),
    "get_updates": (
        _GetUpdates,
        "Read unacknowledged messages. Reads do not acknowledge them. Deduplicate by message ID.",
    ),
    "acknowledge": (
        _Acknowledge,
        "Acknowledge messages only after processing them. Repeated acknowledgements are safe.",
    ),
}

# Only known public API details can reach the model. A proxy or failed server
# can return credentials, URLs, HTML, or validation input in its error body.
_PUBLIC_DETAILS = {
    "A valid Bearer credential is required.",
    "Administrator access is required.",
    "An Agent credential is required.",
    "Cluster not found.",
    "Thread not found.",
    "One or more messages were not found.",
    "Invalid history cursor.",
    "The request ID was already used for a different write.",
    "The database is unavailable. Retry later.",
}
_STATUS_DETAILS = {
    401: "The Agent credential was rejected.",
    403: "This operation is not permitted.",
    404: "The requested board record was not found.",
    409: "The operation conflicts with an existing record.",
    422: "The board rejected the request arguments.",
    429: "The board request limit was reached. Retry later.",
    503: "The board is unavailable. Retry later.",
}


def _error(status: int, detail: str) -> dict:
    return {"ok": False, "error": {"status": status, "detail": detail}}


def _api_error(response: httpx.Response) -> dict:
    detail = _STATUS_DETAILS.get(response.status_code, "The board request failed.")
    try:
        body = response.json()
    except (ValueError, UnicodeDecodeError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        if body["detail"] in _PUBLIC_DETAILS:
            detail = body["detail"]
    return _error(response.status_code, detail)


class BoardTools:
    """Expose six board tools without exposing credentials or identity controls."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cluster_id: str,
        operation_namespace: UUID | None = None,
    ) -> None:
        self.client = client
        self.cluster_id = str(_uuid(cluster_id))
        if operation_namespace is not None and not isinstance(operation_namespace, UUID):
            raise ValueError("The operation namespace must be a UUID.")
        self.operation_namespace = operation_namespace or uuid4()

    def definitions(self) -> list[dict]:
        """Return standard chat-completions function tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in _TOOLS.items()
        ]

    async def execute(self, name: str, arguments: dict, call_id: str) -> dict:
        """Validate and execute one call, with at most two transport/503 retries.

        The same namespace and call ID always give the same write request ID.
        Reusing them with changed content lets the board return a conflict.
        Separate calls do not share a lock or a database transaction.
        """
        if not isinstance(name, str) or name not in _TOOLS:
            return _error(422, "Unknown board tool.")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or len(call_id) > 512
            or any(
                ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in call_id
            )
        ):
            return _error(422, "A nonblank tool call ID of at most 512 characters is required.")
        if not isinstance(arguments, dict):
            return _error(422, "Tool arguments must be an object.")
        try:
            validated = _TOOLS[name][0].model_validate(arguments)
        except ValidationError:
            # Do not send Pydantic's input values or unknown field names back.
            return _error(422, "Invalid tool arguments. Follow the tool's parameter schema.")

        payload = validated.model_dump(mode="json", exclude_none=True)
        path = f"clusters/{self.cluster_id}"
        method = "GET"
        if name == "list_threads":
            path += "/threads"
        elif name == "read_thread":
            path += f"/threads/{payload.pop('thread_id')}"
        elif name == "get_updates":
            path += "/updates"
        elif name == "post_thread":
            path += "/threads"
            method = "POST"
        elif name == "reply":
            path += f"/threads/{payload.pop('thread_id')}/replies"
            method = "POST"
        else:
            path += "/acknowledgements"
            method = "POST"
        if name in {"post_thread", "reply"}:
            payload["request_id"] = str(uuid5(self.operation_namespace, call_id))
        request = {"params" if method == "GET" else "json": payload}

        for attempt in range(3):
            try:
                response = await self.client.request(
                    method, path, **request, follow_redirects=False
                )
            except httpx.RequestError:
                if attempt == 2:
                    return _error(
                        503, "The board could not be reached. Retry this tool call later."
                    )
            else:
                if response.status_code != 503 or attempt == 2:
                    if not response.is_success:
                        return _api_error(response)
                    try:
                        data = response.json()
                    except (ValueError, UnicodeDecodeError):
                        return _error(502, "The board returned an invalid response.")
                    if not isinstance(data, dict):
                        return _error(502, "The board returned an invalid response.")
                    if name == "list_threads":
                        items = data.get("items")
                        if not isinstance(items, list) or any(
                            not isinstance(item, dict) for item in items
                        ):
                            return _error(502, "The board returned an invalid response.")
                        data = {
                            **data,
                            "items": [
                                {key: value for key, value in item.items() if key != "body"}
                                for item in items
                            ],
                        }
                    return {"ok": True, "data": data}
            await asyncio.sleep(0.1 * (attempt + 1))
        raise AssertionError("The bounded request loop must return a result.")
