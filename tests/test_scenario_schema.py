"""Scenario compilation rejects invalid state before a run can start."""

import json

import pytest
from pydantic import ValidationError

from agentrpg.scenario_schema import ScenarioSpec, vehicle_scenario


@pytest.mark.parametrize("count", [1, 2, 3, 15])
def test_same_scenario_structure_for_every_population(count):
    spec = vehicle_scenario(count)
    assert len(spec.owners) == count
    assert len(spec.world.assets) == count + 1
    assert ScenarioSpec.model_validate_json(spec.canonical_json()) == spec
    assert spec.content_hash() == ScenarioSpec.model_validate(spec.model_dump()).content_hash()


@pytest.mark.parametrize("count", [0, 16, True])
def test_population_is_bounded(count):
    with pytest.raises(ValueError):
        vehicle_scenario(count)


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data["owners"].append(data["owners"][0]),
        lambda data: data["world"]["balances"].pop("owner-2"),
        lambda data: data["world"]["balances"].update({"owner-1": -1}),
        lambda data: data["world"]["assets"][0].update(initial_holder="unknown"),
        lambda data: data["owners"][0].update(known_fact_ids=["need-2"]),
        lambda data: data["owners"][0].update(known_fact_ids=["missing"]),
        lambda data: data["owners"][0].update(max_spend=21),
        lambda data: data["facts"][1].update(owner_ids=[]),
        lambda data: data["beats"][0].update(recipients=["unknown"]),
        lambda data: data["beats"][0].update(requires=["missing"]),
        lambda data: data["beats"][0].update(requires=["initial-2"]),
        lambda data: data["beats"][-1]["trigger"].update(window=8),
        lambda data: data["beats"][-1].update(expires_window=4),
        lambda data: data["beats"][0]["trigger"].update(kind="eval_python", code="print(1)"),
        lambda data: data["beats"][0]["trigger"].update(result_status="accepted"),
        lambda data: data["world"]["assets"][0].update(capacity=True),
    ],
)
def test_invalid_scenario_rejected(change):
    data = vehicle_scenario().model_dump(mode="json")
    change(data)
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(data)


def test_trigger_prerequisite_cycle_rejected():
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"][2]["requires"] = ["deadline-reminder"]
    data["beats"][3]["requires"] = ["pressure-2"]
    with pytest.raises(ValidationError, match="cycle"):
        ScenarioSpec.model_validate(data)


def test_content_hash_changes_with_policy_and_ignores_json_key_order():
    spec = vehicle_scenario()
    reversed_keys = dict(reversed(list(spec.model_dump(mode="json").items())))
    assert (
        ScenarioSpec.model_validate_json(json.dumps(reversed_keys)).content_hash()
        == spec.content_hash()
    )
    changed = spec.model_dump(mode="json")
    changed["seed"] += 1
    assert ScenarioSpec.model_validate(changed).content_hash() != spec.content_hash()


def test_false_belief_keeps_separate_world_truth():
    data = vehicle_scenario().model_dump(mode="json")
    data["owners"][0]["beliefs"] = [
        {"fact_id": "need-1", "text": "My meeting cannot be delayed.", "is_false": True}
    ]
    spec = ScenarioSpec.model_validate(data)
    assert spec.owners[0].beliefs[0].is_false
    assert spec.facts[1].text != spec.owners[0].beliefs[0].text


def test_bounded_recurring_window_trigger_compiles():
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"][-1]["trigger"]["every_windows"] = 1
    data["beats"][-1]["max_uses"] = 3
    assert ScenarioSpec.model_validate(data).beats[-1].trigger.every_windows == 1


@pytest.mark.parametrize("every_windows", [0, 100, True])
def test_invalid_recurrence_interval_rejected(every_windows):
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"][-1]["trigger"]["every_windows"] = every_windows
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(data)


def test_nonclock_trigger_cannot_have_recurrence_interval():
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"][0]["trigger"]["every_windows"] = 1
    with pytest.raises(ValidationError, match="recurrence interval"):
        ScenarioSpec.model_validate(data)


@pytest.mark.parametrize(
    "kind,fields",
    [
        ("run_started", {"owner_id": "owner-1"}),
        ("window_reached", {"window": 0, "owner_id": "owner-1"}),
        ("owner_message", {"asset_id": "shared-car"}),
        ("owner_message", {"result_status": "accepted"}),
        ("owner_message", {"source": "world"}),
        ("consent_changed", {"asset_id": "shared-car"}),
        ("consent_changed", {"result_status": "withdrawn"}),
        ("reservation_settled", {"result_status": "pending"}),
        ("reservation_settled", {"source": "actor"}),
        ("proposal_received", {}),
    ],
)
def test_unreachable_event_filters_rejected(kind, fields):
    from agentrpg.scenario_schema import Trigger

    with pytest.raises(ValidationError):
        Trigger(kind=kind, **fields)


def test_each_owner_requires_an_initial_cue():
    data = vehicle_scenario().model_dump(mode="json")
    data["beats"][1]["recipients"] = ["owner-1"]
    with pytest.raises(ValidationError, match="Every owner requires"):
        ScenarioSpec.model_validate(data)
