import importlib
import json
from collections import Counter
from pathlib import Path

import pytest


@pytest.fixture
def study(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("try_clock_implicit_trust")


def test_schedule_counterbalances_named_histories_requests_and_repeats(study):
    rows = list(study.schedule())
    assert len(rows) == len({r["case"] for r in rows}) == 16
    counts = Counter((r["rule"], r["good"], r["request_first"]) for r in rows)
    assert len(counts) == 8 and set(counts.values()) == {2}
    for r in rows:
        assert r["temperature"] == 1.0
        a = study.scenario(r)
        opposite = next(p for p in study.DESKS if p != r["good"])
        b = study.scenario({**r, "good": opposite})
        left, right = [json.loads(s.canonical_json()) for s in (a, b)]
        assert left != right
        for item, other in zip(
            left["clock"]["timeline"][:20], right["clock"]["timeline"][:20], strict=True
        ):
            if item["id"].endswith("confirmation"):
                assert item["at"] != other["at"]
                m, n = item["mail"][0], other["mail"][0]
                assert (study.COOP in m["body"]) == (m["sender_owner_id"] == r["good"])
                assert (study.COOP in n["body"]) == (n["sender_owner_id"] == opposite)
                body, mirrored = (x["body"].removesuffix(study.COOP) for x in (m, n))
                prefix, time = body.rsplit("Confirmed ", 1)
                other_prefix, other_time = mirrored.rsplit("Confirmed ", 1)
                assert prefix == other_prefix
                assert time == item["at"].split()[1] + "."
                assert other_time == other["at"].split()[1] + "."
                item["at"] = other["at"]
                m["body"] = n["body"]
        assert left == right
        messages = [x.mail[0] for x in a.clock.timeline[-2:]]
        assert [m.sender_owner_id for m in messages] == list(
            study.ordered(r["request_first"], study.DESKS)
        )
        assert {m.body for m in messages} == {study.REQUEST}
        assert {o.name for o in a.owners} == {"Morgan Lane", "Amy Vale", "Ben Reed"}


def test_rule_changes_only_the_owner_sentence_and_no_raw_ids(study):
    for row in study.schedule():
        specs = [study.scenario({**row, "rule": rule}) for rule in ("discretion", "forbid")]
        left, right = [json.loads(s.canonical_json()) for s in specs]
        a, b = [s["clock"]["timeline"][-3] for s in (left, right)]
        assert b["cue"] == a["cue"] + study.DENY
        for s in specs:
            texts = [o.request for o in s.owners] + [s.clock.timeline[-3].cue]
            assert all(
                "equal" not in txt.lower() and not any(d in txt for d in study.DESKS)
                for txt in texts
            )
        b["cue"] = a["cue"]
        assert left == right


def test_field_counts_use_distinct_complete_values(study):
    assert study.field_names("15:400; Cobalt Lofts") == []
    assert study.field_names("Cobalt Loft, COBALT LOFT; juniper folder") == ["room", "packet"]
    assert len(study.field_names(study.BRIEF)) == 3
