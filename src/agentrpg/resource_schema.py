"""Bounded resource and owner commerce permission contracts."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ResourceID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]
Money = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Quantity = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
TRADE_VERSION_DESCRIPTION = (
    "Optional version check for the exact trade target. For sell, use the owned item's "
    "version from read_resources or fetch_resource(resource_id). For buy, use the listing's "
    "version from inspect_market or fetch_resource(listing_id). Never use consent.version, "
    "a document or log version, or a global revision. Omit this field when unknown."
)


class ResourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceSpec(ResourceContract):
    id: ResourceID
    owner_id: ResourceID
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    kind: Literal["document", "item"] = "document"
    content: Annotated[str, StringConstraints(max_length=12000)] = ""
    quantity: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    visibility: Literal["private", "shared", "public"] = "private"
    shared_owner_ids: tuple[ResourceID, ...] = Field(default=(), max_length=14)
    agent_writable: bool = True

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if self.kind == "document" and self.quantity:
            raise ValueError("A document has no item quantity.")
        if self.shared_owner_ids and self.visibility != "shared":
            raise ValueError("Shared owner IDs require shared visibility.")
        if len(set(self.shared_owner_ids)) != len(self.shared_owner_ids):
            raise ValueError("Shared owner IDs must be unique.")
        if self.owner_id in self.shared_owner_ids:
            raise ValueError("A resource owner already has access.")
        return self


class SellLimit(ResourceContract):
    resource_id: ResourceID
    quantity: Quantity
    min_unit_price: Money


class CommerceConsent(ResourceContract):
    buy_budget: Money = 0
    buy_items: tuple[ResourceID, ...] = Field(default=(), max_length=100)
    sell_limits: tuple[SellLimit, ...] = Field(default=(), max_length=100)
    expires_window: int | None = Field(default=None, strict=True, ge=0, le=100)

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if self.buy_budget and not self.buy_items:
            raise ValueError("A buy budget requires explicit item IDs.")
        if len(set(self.buy_items)) != len(self.buy_items):
            raise ValueError("Buy item IDs must be unique.")
        if len({item.resource_id for item in self.sell_limits}) != len(self.sell_limits):
            raise ValueError("Sell limits must identify different resources.")
        return self


class ResourceRequest(ResourceContract):
    """A typed owner request. Content edits and access changes use separate operations.

    Create makes a private document. Update changes its content and optional title;
    both require visibility=null and shared_owner_ids=[]. Share changes access only.
    These requests cannot create items or change inventory.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"kind": {"const": "create"}}},
                    "then": {
                        "required": ["title", "content"],
                        "properties": {
                            "resource_id": {"type": "null"},
                            "expected_version": {"type": "null"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                            "visibility": {"type": "null"},
                            "shared_owner_ids": {"maxItems": 0},
                        },
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "update"}}},
                    "then": {
                        "required": ["resource_id", "expected_version", "content"],
                        "properties": {
                            "resource_id": {"type": "string"},
                            "expected_version": {"type": "integer"},
                            "content": {"type": "string"},
                            "visibility": {"type": "null"},
                            "shared_owner_ids": {"maxItems": 0},
                        },
                    },
                },
                {
                    "if": {"properties": {"kind": {"const": "share"}}},
                    "then": {
                        "required": ["resource_id", "expected_version", "visibility"],
                        "properties": {
                            "resource_id": {"type": "string"},
                            "expected_version": {"type": "integer"},
                            "title": {"type": "null"},
                            "content": {"type": "null"},
                            "visibility": {"enum": ["private", "shared", "public"]},
                        },
                    },
                },
                {
                    "if": {"properties": {"visibility": {"enum": [None, "private", "public"]}}},
                    "then": {"properties": {"shared_owner_ids": {"maxItems": 0}}},
                },
            ],
            "examples": [
                {
                    "kind": "create",
                    "title": "Private note",
                    "content": "New text.",
                    "resource_id": None,
                    "expected_version": None,
                    "visibility": None,
                    "shared_owner_ids": [],
                },
                {
                    "kind": "update",
                    "resource_id": "own-note",
                    "expected_version": 2,
                    "content": "Revised text.",
                    "visibility": None,
                    "shared_owner_ids": [],
                },
                {
                    "kind": "share",
                    "resource_id": "own-note",
                    "expected_version": 3,
                    "title": None,
                    "content": None,
                    "visibility": "shared",
                    "shared_owner_ids": ["owner-2"],
                },
            ],
        }
    )

    kind: Literal["create", "update", "share"] = Field(
        description="create: private new document; update: content/title only; share: access only."
    )
    resource_id: ResourceID | None = None
    title: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
        | None
    ) = None
    content: Annotated[str, StringConstraints(max_length=12000)] | None = None
    expected_version: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        description="Exact resource version for update/share; null for create.",
    )
    visibility: Literal["private", "shared", "public"] | None = Field(
        default=None,
        description="Only for kind=share. For create/update, omit or use null even if the "
        "resource is currently private. Update preserves its current access. To change access, "
        "send a separate share request.",
    )
    shared_owner_ids: tuple[ResourceID, ...] = Field(
        default=(),
        max_length=14,
        json_schema_extra={"uniqueItems": True},
        description="Recipient owner IDs only for kind=share with visibility=shared. "
        "Use [] for create/update or for private/public visibility.",
    )

    @model_validator(mode="after")
    def check_request(self) -> Self:
        if self.kind == "create":
            if (
                self.resource_id is not None
                or self.expected_version is not None
                or self.title is None
                or self.content is None
            ):
                raise ValueError(
                    "Create needs a title and content, without an existing ID or version."
                )
        elif self.resource_id is None or self.expected_version is None:
            raise ValueError("Update and share require a resource ID and its current version.")
        if self.kind == "update" and self.content is None:
            raise ValueError("Update needs content.")
        if self.kind == "share":
            if self.visibility is None or self.title is not None or self.content is not None:
                raise ValueError(
                    "Share needs visibility='private', 'shared', or 'public' and "
                    "content=null, title=null. Use a separate update request to change text."
                )
        elif self.visibility is not None or self.shared_owner_ids:
            raise ValueError(
                f"{self.kind.capitalize()} requires visibility=null and shared_owner_ids=[]. "
                "Omit these fields or use those exact empty values. Do not copy the resource's "
                "current visibility. Only a separate kind='share' request can change access."
            )
        if self.shared_owner_ids and self.visibility != "shared":
            raise ValueError("Shared owner IDs require shared visibility.")
        if len(set(self.shared_owner_ids)) != len(self.shared_owner_ids):
            raise ValueError("Shared owner IDs must be unique.")
        return self
