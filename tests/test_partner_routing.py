import importlib
import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest


@pytest.fixture
def routing(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("try_clock_partner_routing")


def test_only_the_directory_order_changes_in_a_matched_replication(routing):
    base, mirror = (list(routing.schedule(first)) for first in routing.DESKS)
    assert len(base) == 24
    for before, after in zip(base, mirror, strict=True):
        assert before["case"] == after["case"]
        original, changed = (
            json.loads(routing.scenario_for(row).canonical_json()) for row in (before, after)
        )
        assert [o["id"] for o in changed["owners"]] == ["lead", "desk-b", "desk-a"]
        changed["owners"][1:] = reversed(changed["owners"][1:])
        assert original == changed


def test_identical_histories_feed_independent_task_probes(routing):
    plan = list(routing.schedule())
    assert len({r["case"] for r in plan}) == len(plan)
    counts = Counter((r["history"], r["task"]) for r in plan)
    assert len(counts) == 6 and set(counts.values()) == {4}
    for i in range(0, len(plan), 2):
        a, b = (routing.scenario_for(row) for row in plan[i : i + 2])
        assert a.owners == b.owners and a.clock.timeline[:-1] == b.clock.timeline[:-1]
        assert a.clock.timeline[-1].cue != b.clock.timeline[-1].cue
        assert a.clock.timeline[-1].at == b.clock.timeline[-1].at


def test_history_manipulates_latency_with_equal_exposure_and_chronology(routing):
    for first in routing.DESKS:
        reference = [(n, desk, start) for n, desk, start, _ in routing.exchanges("equal", first)]
        assert Counter(desk for _, desk, _ in reference) == {"desk-a": 5, "desk-b": 5}
        for history in (*routing.DESKS, "equal"):
            training = list(routing.exchanges(history, first))
            assert [(n, desk, start) for n, desk, start, _ in training] == reference
            assert all(
                end - start == (4 if history == "equal" else 2 if desk == history else 6)
                for _, desk, start, end in training
            )
            assert all(prev[3] < nxt[2] for prev, nxt in pairwise(training))
            assert training[-1][3] < routing.PROBE
