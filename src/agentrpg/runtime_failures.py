"""Conservative, credential-free failure classification for saved participant jobs."""

from dataclasses import dataclass

import httpx
from psycopg import OperationalError
from psycopg.errors import DeadlockDetected, SerializationFailure
from pydantic import ValidationError

HOLDING_KINDS = frozenset(
    {
        "provider_transient",
        "timeout",
        "tool_transient",
        "transport_transient",
        "database_transient",
        "session_busy",
    }
)
PAUSING_KINDS = frozenset({"provider_rejected"})
OUTAGE_KINDS = HOLDING_KINDS | {"provider_request_refused"}


@dataclass(frozen=True)
class Failure:
    kind: str
    retryable: bool = False

    @property
    def holds(self) -> bool:
        return self.kind in HOLDING_KINDS

    @property
    def pauses(self) -> bool:
        return self.kind in PAUSING_KINDS

    @property
    def counts_as_outage(self) -> bool:
        return self.kind in OUTAGE_KINDS


def failure_detail(error: Exception) -> str:
    """For the saved event: a class and a place, or a runtime error's fixed sentence."""
    from agentrpg.actor import ModelGenerationError
    from agentrpg.deep_agent import AgentRuntimeError

    if detail := getattr(error, "detail", None):
        return str(detail)[:300]
    # Only these two: they hold fixed sentences and a status code, never model or provider text.
    if isinstance(error, (AgentRuntimeError, ModelGenerationError)):
        return str(error)[:300]
    return type(error).__name__


def classify_failure(error: Exception) -> Failure:
    from agentrpg.actor import ActorContractError, ModelGenerationError
    from agentrpg.agent_model import ProviderCallError
    from agentrpg.budget import BudgetError
    from agentrpg.deep_agent import (
        AgentBudgetError,
        AgentBusyError,
        AgentIdentityError,
        AgentLimitError,
        AgentProviderError,
        AgentTaskConflictError,
        AgentTransientError,
    )

    if isinstance(error, (BudgetError, AgentBudgetError)):
        return Failure("budget")
    if isinstance(error, ModelGenerationError):
        return Failure(error.kind, error.kind in {"timeout", "provider_transient"})
    if isinstance(error, AgentTransientError):
        return Failure(error.failure_kind, True)
    if isinstance(error, ProviderCallError):
        return Failure(error.kind, error.retryable)
    if isinstance(error, AgentProviderError):
        return Failure(error.kind)
    if isinstance(error, AgentBusyError):
        return Failure("session_busy", True)
    if isinstance(error, AgentLimitError):
        return Failure("task_limit")
    if isinstance(error, (AgentIdentityError, AgentTaskConflictError)):
        return Failure("identity_or_input_conflict")
    if isinstance(error, (ActorContractError, ValidationError, ValueError)):
        return Failure("contract")
    if isinstance(error, httpx.TransportError):
        return Failure("transport_transient", True)
    if isinstance(error, (OperationalError, DeadlockDetected, SerializationFailure)):
        return Failure("database_transient", True)
    return Failure("unknown")
