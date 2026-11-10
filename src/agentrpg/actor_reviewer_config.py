"""Actor/reviewer-only OpenRouter credentials; frozen metadata contains no key."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from agentrpg.personal_agent_config import OPENROUTER_BASE_URL

ACTOR_REVIEWER_ENV = ".env.actor-reviewer.local"


class ActorReviewerConfigError(ValueError):
    """A local configuration error that never contains supplied values."""


@dataclass(frozen=True)
class ActorReviewerAPI:
    model: str
    api_key: SecretStr = field(repr=False)
    credential_source: str
    isolated: bool = False

    def manifest_fields(self) -> dict:
        if not self.isolated:
            return {}
        return {
            "actor_reviewer_api": {
                "provider": "openrouter",
                "base_url": OPENROUTER_BASE_URL,
                "credential_source": self.credential_source,
            }
        }


def load_actor_reviewer_api(
    env: Mapping,
    *,
    model: str,
    root: Path | None = None,
    configuration: Mapping | None = None,
    require_key: bool = True,
) -> ActorReviewerAPI:
    """Select one key for two independent model clients, without changing models.

    The sidecar supplies ACTOR_REVIEWER_* only. Environment values take priority.
    Legacy frozen configurations ignore the new sidecar. An enabled missing key
    fails instead of silently returning to the old provider account.
    """
    frozen = configuration is not None
    isolated = frozen and "actor_reviewer_api" in configuration
    local = {}
    if not frozen or isolated:
        if root is not None:
            try:
                local = dict(dotenv_values(root / ACTOR_REVIEWER_ENV, interpolate=False))
            except (OSError, ValueError):
                raise ActorReviewerConfigError("Cannot read the Actor/reviewer API file.") from None
        local = {
            **{k: v for k, v in local.items() if k.startswith("ACTOR_REVIEWER_")},
            **{k: v for k, v in env.items() if k.startswith("ACTOR_REVIEWER_")},
        }
    if not frozen:
        enabled = str(local.get("ACTOR_REVIEWER_API_ENABLED") or "false").lower()
        if enabled not in {"true", "false"}:
            raise ActorReviewerConfigError("ACTOR_REVIEWER_API_ENABLED must be true or false.")
        isolated = enabled == "true"
    source = "OPENROUTER_API_KEY"
    if isolated:
        if frozen:
            route = configuration["actor_reviewer_api"]
            if (
                not isinstance(route, dict)
                or set(route) != {"provider", "base_url", "credential_source"}
                or route.get("provider") != "openrouter"
                or route.get("base_url") != OPENROUTER_BASE_URL
                or route.get("credential_source")
                not in {"OPENROUTER_API_KEY", "ACTOR_REVIEWER_API_KEY"}
            ):
                raise ActorReviewerConfigError("The saved Actor/reviewer API route is invalid.")
            source = route["credential_source"]
        else:
            if (
                local.get("ACTOR_REVIEWER_PROVIDER") != "openrouter"
                or local.get("ACTOR_REVIEWER_BASE_URL") != OPENROUTER_BASE_URL
            ):
                raise ActorReviewerConfigError("Actor/reviewer overrides support OpenRouter only.")
            if local.get("ACTOR_REVIEWER_MODEL", model) != model:
                raise ActorReviewerConfigError("Keep the original Actor/reviewer model ID.")
            source = "ACTOR_REVIEWER_API_KEY"
    if frozen and configuration.get("model") != model:
        raise ActorReviewerConfigError("The saved Actor/reviewer model differs from the baseline.")
    key = local.get(source) if source == "ACTOR_REVIEWER_API_KEY" else env.get(source)
    if require_key and (not isinstance(key, str) or not key.strip()):
        raise ActorReviewerConfigError("Set the selected Actor/reviewer API key locally.")
    return ActorReviewerAPI(model, SecretStr(key or ""), source, isolated)
