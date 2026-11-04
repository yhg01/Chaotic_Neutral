"""Frozen per-owner assistant selection; owner Actor models remain independent."""

import re


def owner_models(entries):
    result = {}
    for entry in entries or ():
        if not isinstance(entry, str) or entry.count("=") != 1:
            raise ValueError("Use OWNER_ID=MODEL_ID for each assistant assignment.")
        owner, model = entry.split("=", 1)
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", owner)
            or not 1 <= len(model) <= 200
            or any(c.isspace() for c in model)
        ):
            raise ValueError("Invalid owner or model ID.")
        if owner in result:
            raise ValueError("Each owner can have only one assistant model assignment.")
        result[owner] = model
    if len(result) > 15:
        raise ValueError("A cluster supports at most fifteen assistant assignments.")
    return result


def validate_owner_models(mapping, owners):
    if not isinstance(mapping, dict):
        raise ValueError("Owner assistant models must be a mapping.")
    parsed = owner_models([f"{key}={value}" for key, value in mapping.items()])
    if not set(parsed) <= set(owners) or any(not isinstance(v, str) for v in mapping.values()):
        raise ValueError("Assistant models must name existing owners and model IDs.")
    return parsed
