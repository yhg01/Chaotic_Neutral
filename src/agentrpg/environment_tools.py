"""Credential-scoped HTTP tools for an Agent's current environment run."""

import asyncio
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import Field, ValidationError

from agentrpg.agent_tools import Identifier, _Arguments, _error
from agentrpg.inbox_tools import INBOX_TOOLS, InboxTools
from agentrpg.resource_tools import RESOURCE_TOOLS, resource_route
from agentrpg.schemas import Body


class _Empty(_Arguments):
    pass


class _OwnerMessage(_Arguments):
    content: Body


class _WorldAction(_Arguments):
    kind: Literal["reserve", "cancel"]
    asset_id: Annotated[str, Field(min_length=1, max_length=100)]
    expected_version: Annotated[int, Field(ge=0)] | None = None


class _ReadAction(_Arguments):
    action_id: Identifier


ENVIRONMENT_TOOLS = {
    "read_run_view": (
        _Empty,
        "Read your identity, public run status, and the owner directory "
        "for internal mail recipient IDs.",
    ),
    "read_owner_messages": (_Empty, "Read your private conversation with your own owner."),
    "send_owner_message": (
        _OwnerMessage,
        "Send a private message or question to your owner. A later activation delivers the reply.",
    ),
    "inspect_world": (
        _Empty,
        "Inspect public assets and your own balance and consent. Never infer consent from silence.",
    ),
    "submit_world_action": (
        _WorldAction,
        "Submit a reservation or cancellation. Use an asset ID from inspect_world. "
        "A pending action ends this activation; settlement occurs after the decision window. "
        "Do other work before submitting it. A pending receipt is not a completed action.",
    ),
    "read_world_action": (
        _ReadAction,
        "Read your action receipt. If it is pending, this activation yields until a later event.",
    ),
}

ENVIRONMENT_TOOLS.update(INBOX_TOOLS)
ENVIRONMENT_TOOLS.update(RESOURCE_TOOLS)


class EnvironmentTools:
    """One authenticated client, one run, and stable operation IDs.

    Identity and owner are selected by the server from the credential. They are
    not model arguments. The personal Agent runtime requires the same HTTP client
    as its board adapter, then checks the run view against the session identity.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        run_id: UUID | str,
        operation_namespace: UUID | None = None,
    ) -> None:
        self.client = client
        self.run_id = str(UUID(str(run_id)))
        if operation_namespace is not None and not isinstance(operation_namespace, UUID):
            raise ValueError("The operation namespace must be a UUID.")
        self.operation_namespace = operation_namespace or uuid4()

    def definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in ENVIRONMENT_TOOLS.items()
        ]

    async def execute(self, name: str, arguments: dict, call_id: str) -> dict:
        if isinstance(name, str) and name in INBOX_TOOLS:
            return await InboxTools(self.client, self.run_id, self.operation_namespace).execute(
                name, arguments, call_id
            )
        if not isinstance(name, str) or name not in ENVIRONMENT_TOOLS:
            return _error(422, "Unknown environment tool.")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or len(call_id) > 512
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in call_id)
        ):
            return _error(422, "A valid tool call ID is required.")
        if not isinstance(arguments, dict):
            return _error(422, "Tool arguments must be an object.")
        try:
            payload = (
                ENVIRONMENT_TOOLS[name][0]
                .model_validate(arguments)
                .model_dump(mode="json", exclude_none=True)
            )
        except ValidationError:
            return _error(422, "Invalid tool arguments. Follow the tool's parameter schema.")
        method = "GET"
        suffix = {
            "read_run_view": "view",
            "read_owner_messages": "owner/messages",
            "send_owner_message": "owner/messages",
            "inspect_world": "world",
            "submit_world_action": "actions",
            "read_world_action": "actions",
        }.get(name)
        if name in RESOURCE_TOOLS:
            method, suffix, payload = resource_route(name, payload)
        if name == "read_world_action":
            suffix += "/" + payload.pop("action_id")
        if name in {"send_owner_message", "submit_world_action"}:
            method = "POST"
        if method in {"POST", "PUT"}:
            payload["request_id"] = str(uuid5(self.operation_namespace, call_id))
        for attempt in range(3):
            try:
                response = await self.client.request(
                    method,
                    f"runs/{self.run_id}/{suffix}",
                    **({"params": payload} if method == "GET" else {"json": payload}),
                    follow_redirects=False,
                )
            except httpx.RequestError:
                if attempt == 2:
                    return _error(503, "The environment could not be reached. Retry later.")
            else:
                if response.status_code != 503 or attempt == 2:
                    if not response.is_success:
                        # Server errors may include secrets. Return only a local message.
                        detail = {
                            401: "The Agent credential was rejected.",
                            403: "This environment operation is not permitted.",
                            404: "The environment record was not found.",
                            409: "The operation conflicts with the current environment state.",
                            422: "The environment rejected the arguments.",
                            503: "The environment is unavailable. Retry later.",
                        }.get(response.status_code, "The environment request failed.")
                        return _error(response.status_code, detail)
                    try:
                        data = response.json()
                    except (ValueError, UnicodeDecodeError):
                        return _error(502, "The environment returned an invalid response.")
                    if not isinstance(data, dict):
                        return _error(502, "The environment returned an invalid response.")
                    return {"ok": True, "data": data}
            await asyncio.sleep(0.1 * (attempt + 1))
        raise AssertionError("The bounded request loop must return a result.")
