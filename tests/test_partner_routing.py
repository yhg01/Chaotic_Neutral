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


def test_history_check_only_adds_the_instruction_to_use_past_latency(routing):
    checks = list(routing.history_check_schedule())
    assert len(checks) == 8
    assert Counter((r["task"], r["history"]) for r in checks) == {
        (t, d): 2 for t in routing.TASKS for d in routing.DESKS
    }
    for row in checks:
        baseline = routing.scenario_for({**row, "use_history": False})
        extra = json.loads(routing.scenario_for(row).canonical_json())
        cue = extra["clock"]["timeline"][-1]
        assert cue["cue"] == baseline.clock.timeline[-1].cue + " " + routing.USE_HISTORY
        cue["cue"] = baseline.clock.timeline[-1].cue
        assert extra == json.loads(baseline.canonical_json())


@pytest.fixture
def routing_v2(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("try_clock_partner_routing_v2")


def test_v2_phases_have_the_planned_sizes_and_unique_cases(routing_v2):
    sizes = {p: len(list(routing_v2.schedule(p))) for p in routing_v2.PHASES}
    assert sizes == {0: 8, 1: 16, 2: 16, 3: 8}
    cases = [r["case"] for p in routing_v2.PHASES for r in routing_v2.schedule(p)]
    assert len(set(cases)) == len(cases)
    for p in (1, 2, 3):
        counts = Counter(
            (r["history"], r["history_first"], r["listed_first"]) for r in routing_v2.schedule(p)
        )
        assert len(counts) == 8 and len(set(counts.values())) == 1


def test_v2_owner_uses_display_names_and_states_no_tie_by_default(routing_v2):
    for row in routing_v2.schedule(1):
        cue = row["cue"]
        assert "harbor-desk" not in cue and "linden-desk" not in cue
        assert "response time" not in cue and "as soon as possible" not in cue
        first, second = (
            routing_v2.NAMES[d] for d in routing_v2.ordered(row["listed_first"], routing_v2.DESKS)
        )
        assert cue.index(first) < cue.index(second)


def test_v2_arms_differ_by_one_sentence(routing, routing_v2):
    neutral = {
        (r["history"], r["history_first"], r["listed_first"]): r for r in routing_v2.schedule(1)
    }
    for p, extra in ((2, routing_v2.URGENT), (3, routing_v2.TIE)):
        for row in routing_v2.schedule(p):
            base = neutral[row["history"], row["history_first"], row["listed_first"]]
            assert row["cue"].replace(" " + extra, "") == base["cue"]
            changed = json.loads(routing.scenario_for(row).canonical_json())
            changed["clock"]["timeline"][-1]["cue"] = base["cue"]
            assert changed == json.loads(routing.scenario_for(base).canonical_json())


def test_v2_worlds_use_neutral_desks_with_names(routing, routing_v2):
    row = next(routing_v2.schedule(1))
    spec = routing.scenario_for(row)
    names = {o.id: o.name for o in spec.owners}
    assert names["harbor-desk"] == "Harbor Desk" and names["linden-desk"] == "Linden Desk"
    senders = {t.mail[0].sender_owner_id for t in spec.clock.timeline[:-1]}
    assert senders == {"lead", "harbor-desk", "linden-desk"}


def test_v2_matched_swap_changes_only_confirmation_times(routing, routing_v2):
    rows = {
        (r["history"], r["history_first"], r["listed_first"], r["repeat"]): r
        for r in routing_v2.schedule(1)
    }
    fast, slow = routing_v2.DESKS
    for (history, hf, nf, rep), row in rows.items():
        if history != fast:
            continue
        a = routing.scenario_for(row)
        b = routing.scenario_for(rows[slow, hf, nf, rep])
        assert a.owners == b.owners and a.clock.timeline[-1] == b.clock.timeline[-1]
        for x, y in zip(a.clock.timeline[:-1], b.clock.timeline[:-1], strict=True):
            assert x.mail[0].subject == y.mail[0].subject
            assert x.mail[0].sender_owner_id == y.mail[0].sender_owner_id


def test_v1_defaults_are_unchanged_by_the_desk_table(routing):
    row = next(routing.schedule())
    assert routing.desks_of(row) == routing.DESKS
    cue = routing.scenario_for(row).clock.timeline[-1].cue
    assert cue.startswith(routing.TASKS[row["task"]]) and "same response time" in cue
