"""An owner's exact one-payment consent, with identities supplied by the runtime."""

from typing import Annotated

from pydantic import Field, StringConstraints

from agentrpg.scenario_schema import Contract, Identifier


class TransferConsent(Contract):
    task_key: Identifier | None = Field(
        default=None, description="Existing owner task key when transfer task tracking is enabled."
    )
    recipient_owner_id: Identifier
    amount: int = Field(strict=True, ge=1, le=1_000_000)
    memo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
