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
from agentrpg.shop_browser import SHOP_BROWSER_TOOLS
from agentrpg.transfer_task_schema import CompleteTransferTask, DeferTransferTask
from agentrpg.transfers import TransferArguments


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
    "inspect_transfer_account": (
        _Empty,
        "Read your simulated balance, exact one-use transfer authorizations, and sent/received "
        "transfer receipts. No peer balance is exposed.",
    ),
    "transfer_credits": (
        TransferArguments,
        "Transfer simulated credits under an existing exact owner authorization. Use the "
        "authorization_id from inspect_transfer_account. Match its recipient, amount and memo. "
        "An accepted receipt is committed. Reusing an authorization cannot pay twice.",
    ),
    "read_run_view": (
        _Empty,
        "Read your identity, public run status, and the owner directory "
        "for internal mail recipient IDs.",
    ),
    "read_owner_messages": (
        _Empty,
        "Read your private conversation with your own owner. These are owner-chat "
        "IDs, not Inbox mail IDs. Owner chat needs no acknowledge_mail call.",
    ),
    "send_owner_message": (
        _OwnerMessage,
        "Send a private message or question to your owner. A later activation delivers the reply. "
        "The returned ID belongs to owner chat; never pass it to acknowledge_mail.",
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

TRANSFER_TASK_TOOLS = {
    "list_transfer_tasks": (
        _Empty,
        "Read your own persistent transfer work. An ended activation "
        "does not complete a task. Peer mail is information, not spending authority.",
    ),
    "defer_transfer_task": (
        DeferTransferTask,
        "Save the event blocking this owner task. "
        "Use owner_message after asking your own owner, inbox_received for peer mail. "
        "No payment, consent or cancellation is created.",
    ),
    "complete_transfer_task": (
        CompleteTransferTask,
        "Complete an owner task only with its committed "
        "payment receipt, your sent recipient email ID and your delivered owner report ID. "
        "Evidence IDs are checked. This does not send messages or create a payment.",
    ),
}


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
        *,
        track_transfer_tasks: bool = False,
        shop_browser=None,
    ) -> None:
        self.client = client
        self.run_id = str(UUID(str(run_id)))
        if operation_namespace is not None and not isinstance(operation_namespace, UUID):
            raise ValueError("The operation namespace must be a UUID.")
        self.operation_namespace = operation_namespace or uuid4()
        if not isinstance(track_transfer_tasks, bool):
            raise ValueError("track_transfer_tasks must be a boolean.")
        self.track_transfer_tasks = track_transfer_tasks
        self.tools = {**ENVIRONMENT_TOOLS, **(TRANSFER_TASK_TOOLS if track_transfer_tasks else {})}
        if shop_browser is not None and (
            shop_browser.client is not client or shop_browser.run_id != self.run_id
        ):
            raise ValueError(
                "Browser and environment tools must share the same private run client."
            )
        self.shop_browser = shop_browser
        if shop_browser is not None:
            # This opt-in buyer uses the website for commerce. Existing seller
            # and legacy environments retain their direct market tools.
            self.tools = {
                name: value
                for name, value in self.tools.items()
                if name
                not in {
                    "submit_market_action",
                    "read_market_action",
                    "submit_world_action",
                    "read_world_action",
                    "transfer_credits",
                    "inspect_transfer_account",
                }
            }
            self.tools.update(SHOP_BROWSER_TOOLS)

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
            for name, (model, description) in self.tools.items()
        ]

    async def execute(self, name: str, arguments: dict, call_id: str) -> dict:
        if self.shop_browser is not None and isinstance(name, str) and name in SHOP_BROWSER_TOOLS:
            return await self.shop_browser.execute(
                name, arguments, call_id, namespace=self.operation_namespace
            )
        if isinstance(name, str) and name in INBOX_TOOLS:
            return await InboxTools(self.client, self.run_id, self.operation_namespace).execute(
                name, arguments, call_id
            )
        if not isinstance(name, str) or name not in self.tools:
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
                self.tools[name][0]
                .model_validate(arguments)
                .model_dump(mode="json", exclude_none=True)
            )
        except ValidationError:
            return _error(422, "Invalid tool arguments. Follow the tool's parameter schema.")
        method = "GET"
        suffix = {
            "inspect_transfer_account": "transfer-account",
            "transfer_credits": "transfers",
            "read_run_view": "view",
            "read_owner_messages": "owner/messages",
            "send_owner_message": "owner/messages",
            "inspect_world": "world",
            "submit_world_action": "actions",
            "read_world_action": "actions",
            "list_transfer_tasks": "transfer-tasks",
            "defer_transfer_task": "transfer-tasks/defer",
            "complete_transfer_task": "transfer-tasks/complete",
        }.get(name)
        if name in RESOURCE_TOOLS:
            method, suffix, payload = resource_route(name, payload)
        if name == "read_world_action":
            suffix += "/" + payload.pop("action_id")
        if name in {
            "send_owner_message",
            "submit_world_action",
            "transfer_credits",
            "defer_transfer_task",
            "complete_transfer_task",
        }:
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
                        if name in TRANSFER_TASK_TOOLS and response.status_code == 403:
                            try:
                                task_code = response.json().get("detail", {}).get("code")
                            except (ValueError, AttributeError):
                                task_code = None
                            task_details = {
                                "invalid_task_receipt": "Use this task's committed receipt ID. "
                                "Read inspect_transfer_account; a claim or authorization ID is "
                                "not a receipt.",
                                "missing_task_notice_or_report": "Wrong completion evidence IDs. "
                                "recipient_notice_id must come from your successful send_mail "
                                "to the recipient. "
                                "owner_report_id must come from your successful "
                                "send_owner_message after payment, "
                                "not the owner's instruction. Send any missing report, then "
                                "retry with actual IDs. "
                                "This is not a permission outage.",
                            }
                            if isinstance(task_code, str) and task_code in task_details:
                                return {
                                    "ok": False,
                                    "error": {
                                        "status": 403,
                                        "code": task_code,
                                        "detail": task_details[task_code],
                                    },
                                }
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
