"""Role-scoped OpenRouter selection, with secret-free frozen run metadata."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PERSONAL_AGENT_ENV = ".env.personal-agent.local"
DEFAULT_DEEPSEEK_MODEL = "deepseek/deepseek-v3.2"


class PersonalAgentConfigError(ValueError):
    """A local configuration error with no credential or supplied value."""


@dataclass(frozen=True)
class PersonalAgentAPI:
    model: str
    api_key: SecretStr = field(repr=False)
    credential_source: str
    isolated: bool = False

    def manifest_fields(self) -> dict:
        """Save routing and the key's variable name, never the credential."""
        if not self.isolated:
            return {}
        return {
            "personal_agent_api": {
                "provider": "openrouter",
                "base_url": OPENROUTER_BASE_URL,
                "credential_source": self.credential_source,
            }
        }


def load_personal_agent_api(
    env: Mapping,
    *,
    root: Path | None = None,
    model: str | None = None,
    configuration: Mapping | None = None,
    require_key: bool = True,
) -> PersonalAgentAPI:
    """Select only assistant credentials; an existing run keeps its saved route.

    The private sidecar supplies PERSONAL_AGENT_* values only. The caller's
    environment overrides it. Legacy runs ignore the sidecar entirely.
    """
    local = {}
    frozen = configuration is not None
    isolated = frozen and "personal_agent_api" in configuration
    if not frozen or isolated:
        if root is not None:
            try:
                local = dict(dotenv_values(root / PERSONAL_AGENT_ENV, interpolate=False))
            except (OSError, ValueError):
                raise PersonalAgentConfigError("Cannot read the personal Agent API file.") from None
        local = {**local, **{k: v for k, v in env.items() if k.startswith("PERSONAL_AGENT_")}}
    if not frozen:
        enabled = str(local.get("PERSONAL_AGENT_API_ENABLED") or "false").lower()
        if enabled not in {"true", "false"}:
            raise PersonalAgentConfigError("PERSONAL_AGENT_API_ENABLED must be true or false.")
        isolated = enabled == "true"
    source = "OPENROUTER_API_KEY"
    if isolated:
        if frozen:
            route = configuration["personal_agent_api"]
            if (
                not isinstance(route, dict)
                or set(route) != {"provider", "base_url", "credential_source"}
                or route.get("provider") != "openrouter"
                or route.get("base_url") != OPENROUTER_BASE_URL
                or route.get("credential_source")
                not in {"OPENROUTER_API_KEY", "PERSONAL_AGENT_API_KEY"}
            ):
                raise PersonalAgentConfigError("The saved personal Agent API route is invalid.")
            source = route["credential_source"]
        else:
            if (
                local.get("PERSONAL_AGENT_PROVIDER") != "openrouter"
                or local.get("PERSONAL_AGENT_BASE_URL") != OPENROUTER_BASE_URL
            ):
                raise PersonalAgentConfigError("Personal Agents currently support OpenRouter only.")
            source = "PERSONAL_AGENT_API_KEY"
    key = local.get(source) if source == "PERSONAL_AGENT_API_KEY" else env.get(source)
    if require_key and (not isinstance(key, str) or not key.strip()):
        raise PersonalAgentConfigError("Set the selected personal Agent API key locally.")
    if frozen:
        name = configuration.get("model")
    elif isolated:
        name = model or local.get("PERSONAL_AGENT_MODEL")
    else:
        name = model or env.get("OPENROUTER_MODEL") or "openai/gpt-4.1-mini"
    if not isinstance(name, str) or not 1 <= len(name) <= 200 or any(c.isspace() for c in name):
        raise PersonalAgentConfigError("Set a valid personal Agent model ID.")
    return PersonalAgentAPI(name, SecretStr(key or ""), source, isolated)
