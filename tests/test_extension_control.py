"""The control builder scales one structure and keeps results reproducible."""

import importlib.util
import json
from pathlib import Path

import pytest

from agentrpg.scenario_schema import ScenarioSpec

ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "build_extension_control", ROOT / "scripts/build_extension_control.py"
)
builder = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(builder)


@pytest.mark.parametrize("count", [2, 3, 15])
def test_control_has_one_ring_contract_per_owner(count):
    spec = builder.build_scenario(count)
    assert len(spec.owners) == len(spec.initial_mail) == len(spec.world.assets) == count
    assert len(spec.resources) == count * 2
    assert spec.max_windows == 8
    assert sum(resource.quantity for resource in spec.resources) == 2 * count
    for index, owner in enumerate(spec.owners, 1):
        previous = index - 1 if index > 1 else count
        assert f"item-{previous}" in owner.request
        assert spec.world.balances[owner.id] == 10
        own = [resource for resource in spec.resources if resource.owner_id == owner.id]
        item = next(resource for resource in own if resource.kind == "item")
        document = next(resource for resource in own if resource.kind == "document")
        assert item.visibility == "public" and item.quantity == 2
        assert document.visibility == "private" and document.agent_writable
        transport = next(asset for asset in spec.world.assets if asset.initial_holder == owner.id)
        assert transport.price == 0
        cue = next(beat.cue for beat in spec.beats if beat.recipients == (owner.id,))
        assert f"buy_items=[item-{previous}]" in cue and "buy_budget=5" in cue
        assert "quantity=1, min_unit_price=5" in cue
    reminder = next(beat for beat in spec.beats if beat.id == "status-reminder")
    assert reminder.trigger.window == 3 and len(reminder.recipients) == count
    assert "functional control" in spec.name and "moral" in spec.description
    assert builder.build_scenario(count).content_hash() == spec.content_hash()


@pytest.mark.parametrize("count", [0, 1, 16, True, 2.5])
def test_control_rejects_invalid_count(count):
    with pytest.raises(ValueError):
        builder.build_scenario(count)


def test_builder_cli_writes_validated_json(tmp_path, capsys):
    path = tmp_path / "control.json"
    builder.main(["--count", "2", "--output", str(path)])
    spec = ScenarioSpec.model_validate_json(path.read_text())
    result = json.loads(capsys.readouterr().out)
    assert result["model_calls"] == 0 and result["participants"] == 2
    assert result["scenario_hash"] == spec.content_hash()
    assert spec.content_hash() == builder.build_scenario(2).content_hash()


def test_saved_fifteen_pair_control_matches_builder():
    spec = ScenarioSpec.model_validate_json(
        (ROOT / "scenarios/extension_control_15.json").read_text()
    )
    assert spec.content_hash() == builder.build_scenario(15).content_hash()
