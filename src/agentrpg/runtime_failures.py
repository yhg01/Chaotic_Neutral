"""Conservative, credential-free failure classification for saved participant jobs."""

from dataclasses import dataclass

import httpx
from psycopg import OperationalError
from psycopg.errors import DeadlockDetected, SerializationFailure
from pydantic import ValidationError


@dataclass(frozen=True)
class Failure:
    kind: str
    retryable: bool = False


def classify_failure(error: Exception) -> Failure:
    # Import at use time: Actor and Agent adapters do not depend on the conductor.
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
        return Failure(
            "provider_transient" if error.retryable else "provider_rejected", error.retryable
        )
    if isinstance(error, AgentProviderError):
        return Failure("provider_rejected")
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
    # Do not infer retryability from an exception message, class name, or model text.
    return Failure("unknown")
