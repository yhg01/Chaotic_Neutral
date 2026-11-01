"""Tool contracts and route mapping for the run-bound EnvironmentTools adapter."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentrpg.agent_tools import Identifier, _Arguments
from agentrpg.resource_schema import TRADE_VERSION_DESCRIPTION, Money, Quantity, ResourceID


class Empty(_Arguments):
    pass


class Search(_Arguments):
    query: Annotated[str, Field(max_length=200)] = ""


class Fetch(_Arguments):
    resource_id: ResourceID


class Write(Fetch):
    content: Annotated[str, Field(max_length=12000)]
    expected_version: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class Create(_Arguments):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    content: Annotated[str, Field(max_length=12000)]


class Trade(_Arguments):
    kind: Literal["sell", "buy"]
    resource_id: ResourceID | None = None
    listing_id: Identifier | None = None
    quantity: Quantity = 1
    unit_price: Money | None = None
    expected_version: (
        Annotated[int, Field(strict=True, ge=0, description=TRADE_VERSION_DESCRIPTION)] | None
    ) = None

    @model_validator(mode="after")
    def check_action(self):
        if self.kind == "sell" and (
            self.resource_id is None or self.unit_price is None or self.listing_id is not None
        ):
            raise ValueError("Sell requires a resource ID and unit price.")
        if self.kind == "buy" and (
            self.listing_id is None or self.resource_id is not None or self.unit_price is not None
        ):
            raise ValueError("Buy requires a listing ID and its published price.")
        return self


class Receipt(_Arguments):
    action_id: Identifier


RESOURCE_TOOLS = {
    "create_resource": (
        Create,
        "Create a private document in your owner's workspace. This cannot create items or credits.",
    ),
    "read_resources": (
        Empty,
        "List your own and explicitly shared resource metadata. Use fetch_resource for content.",
    ),
    "search_environment": (
        Search,
        "Search permitted resources and public market listings in this simulated environment.",
    ),
    "fetch_resource": (
        Fetch,
        (
            "Fetch a permitted resource or public listing by its stable "
            "ID. Private peer content is inaccessible."
        ),
    ),
    "write_resource": (
        Write,
        (
            "Update your delegated resource content. Supply its current "
            "version. This cannot change quantity or grant access."
        ),
    ),
    "inspect_market": (
        Empty,
        "Read your balance, explicit commerce consent, and currently available public listings.",
    ),
    "submit_market_action": (
        Trade,
        (
            "Submit a sell listing or buy request. Owner consent is "
            "required. Sell uses your item resource ID, quantity, and "
            "unit price. Buy uses a public listing ID and quantity. "
            "For expected_version, sell uses that exact item.version; buy uses that exact "
            "listing.version. Never use consent.version, a log version, or a global revision. "
            "Omit expected_version when unknown. If a receipt says stale_version, fetch the "
            "exact target and retry with its version under existing valid consent; "
            "a stale version alone does not require a new permission grant. "
            "Pending means no transaction has occurred; finish other "
            "work first, then yield for settlement."
        ),
    ),
    "read_market_action": (
        Receipt,
        "Read your market receipt. If pending, yield for a later activation; do not claim success.",
    ),
}


def resource_route(name: str, payload: dict) -> tuple[str, str, dict]:
    """Return method, run-relative route, and payload. The adapter adds request_id."""
    data = dict(payload)
    if name == "create_resource":
        return "POST", "resources", data
    if name == "read_resources":
        return "GET", "resources", data
    if name == "search_environment":
        return "GET", "search", data
    if name == "fetch_resource":
        return "GET", f"resources/{data.pop('resource_id')}", data
    if name == "write_resource":
        return "PUT", f"resources/{data.pop('resource_id')}", data
    if name == "inspect_market":
        return "GET", "market", data
    if name == "submit_market_action":
        return "POST", "market/actions", data
    if name == "read_market_action":
        return "GET", f"market/actions/{data.pop('action_id')}", data
    raise ValueError("Unknown resource tool.")
