"""Owner purchase tasks constrain a browser checkout without granting authority."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from agentrpg.resource_schema import ResourceID
from agentrpg.scenario_schema import Contract, Identifier


class ShoppingTaskRequest(Contract):
    kind: Literal["open", "cancel"]
    task_key: Identifier
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    item_ids: tuple[ResourceID, ...] = Field(default=(), max_length=20)
    quantity: int | None = Field(default=None, strict=True, ge=1, le=100)
    max_total: int | None = Field(default=None, strict=True, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.kind == "open":
            if (
                not self.description
                or not self.item_ids
                or self.quantity is None
                or self.max_total is None
            ):
                raise ValueError(
                    "A shopping task needs its purpose, permitted items, quantity and total cap."
                )
            if len(set(self.item_ids)) != len(self.item_ids):
                raise ValueError("Shopping item IDs must be unique.")
        elif (
            self.description is not None
            or self.item_ids
            or self.quantity is not None
            or self.max_total is not None
        ):
            raise ValueError("Cancellation names only an existing shopping task key.")
        return self


class OpenShoppingTaskRequest(ShoppingTaskRequest):
    kind: Literal["open"]
    description: str = Field(
        min_length=1,
        max_length=1000,
        description="Required short purpose of this purchase, e.g. buy one ceramic mug.",
    )
    item_ids: tuple[ResourceID, ...] = Field(min_length=1, max_length=20)
    quantity: int = Field(strict=True, ge=1, le=100)
    max_total: int = Field(strict=True, ge=1, le=1_000_000)


class CancelShoppingTaskRequest(ShoppingTaskRequest):
    kind: Literal["cancel"]
    description: None = None
    item_ids: tuple[ResourceID, ...] = Field(default=(), max_length=0)
    quantity: None = None
    max_total: None = None


ShoppingTaskCommand = Annotated[
    OpenShoppingTaskRequest | CancelShoppingTaskRequest, Field(discriminator="kind")
]
