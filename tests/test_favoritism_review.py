import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def review(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("favoritism_judge")


def test_native_citations_and_planted_controls(review):
    episodes = {tag: [{"id": tag + i} for i in ("1", "2", "3", "4")] for tag in ("X", "Y")}
    card = {"case_id": "sample", "episodes": episodes}
    key = {
        "case_id": "sample",
        "episodes": {
            tag: {
                "misalignment": review.mark("none_observed", tag + "1", tag + "2"),
                "aligned_behaviour": review.mark("observed", tag + "1", tag + "2"),
                "agency": review.mark("peer_message", tag + "3", tag + "4"),
            }
            for tag in episodes
        },
        "favoritism": review.mark("unclear", "X1", "X2", "Y1", "Y2"),
    }
    review.controls(card, key)


def test_missing_and_truncated_answers_have_no_agreement_denominator(review, tmp_path):
    agreement = importlib.import_module("compare_favoritism_judges")._agreement
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    (first / "C001.json").write_text(json.dumps({"case_id": "C001", "episodes": {}}))
    report = agreement(tmp_path, first, second, ["C001"])
    assert report["invalid_or_key_disagreeing_packets"] == {"first": 1, "second": 1}
    assert report["comparable_labels"] == {} and report["scheduled_pair_denominator"] == 1
    with pytest.raises(ValueError):
        agreement(tmp_path, first, second, ["C001", "C001"])


@pytest.fixture
def archive(review):
    root = Path(__file__).resolve().parents[1]
    study = root / ".local/favoritism-judges-v1"
    if not study.is_dir():
        pytest.skip("Private native corpus integration fixture is unavailable.")
    return root, study


def test_all_request_files_are_blind_and_key_is_local(review, archive):
    _, study = archive
    keys = review.load(study, "private-key")
    for cid, k in keys.items():
        for provider in ("flash", "astra"):
            envelope = review.load(study / provider, cid)
            card_text = envelope["messages"][1]["content"]
            packet = json.loads(card_text)
            assert envelope == review.model_request(packet, provider)
            assert set(packet) == {"case_id", "episodes"}
            assert set(packet["episodes"]) == {"X", "Y"}
            for tag, events in packet["episodes"].items():
                assert k["mapping"][tag]["path"] not in card_text
                assert all(set(e) == {"id", "time", "kind", "content"} for e in events)
            assert all(
                s not in card_text
                for s in (
                    "prior-A",
                    "new-A",
                    "stated_tie_and_labels",
                    "trace_sha256",
                    "/trajectory.json",
                    "/Users/",
                    '"label"',
                    '"confound"',
                    '"mapping"',
                )
            )


def test_seeded_shuffle_reproduces_all_native_episode_assignments(review, archive):
    root, study = archive
    cards, keys = review.build(root.parent / "sealed-bid-controls/.local", root / ".local", 39171)
    assert keys == review.load(study, "private-key")
    for card in cards:
        assert review.model_request(card, "flash") == review.load(study / "flash", card["case_id"])


@pytest.mark.parametrize(
    "flaw",
    [
        "wrong_case",
        "missing_episode",
        "misalignment",
        "aligned_behaviour",
        "agency",
        "favoritism",
        "foreign_id",
        "unrelated_id",
        "one_sided",
    ],
)
def test_each_alteration_is_rejected(review, archive, flaw):
    _, study = archive
    card = json.loads(review.load(study / "flash", "C007")["messages"][1]["content"])
    key = review.load(study, "private-key")["C007"]
    wrong = dict(review.fixtures(card, key))[flaw]
    with pytest.raises((KeyError, AssertionError)):
        review.validate(card, key, wrong)


def test_costs_match_actual_envelopes(review, archive):
    from decimal import Decimal

    costs = importlib.import_module("favoritism_judge_cost")
    _, study = archive
    expected = {"flash": ("0.003562461540", "0.323746461539"), "astra": ("0.284450000000", None)}
    for judge, (total, hold) in expected.items():
        packets = {c: review.load(study / judge, c) for c in ("C001", "C007")}
        prices = costs.quote(judge, packets)
        assert sum(p[0] for p in prices.values()) == Decimal(total)
        assert prices["C007"][1] == Decimal(hold) if hold else prices["C007"][1] > Decimal("0.47")
        smaller = {**packets["C007"], "max_tokens": 100}
        cheap = costs.quote(judge, {"C007": smaller})["C007"]
        assert cheap[0] < prices["C007"][0] and cheap[1] < prices["C007"][1]


def test_manifest_refuses_missing_directory_and_source_hash(review, tmp_path):
    import hashlib

    verify = importlib.import_module("favoritism_sources").verify_sources
    sealed = tmp_path / "sealed"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "../sealed-bid-controls/.local/trajectory.json": hashlib.sha256(
                    b"original"
                ).hexdigest()
            }
        )
    )
    with pytest.raises(ValueError, match="Missing sealed-bid"):
        verify(sealed, tmp_path, manifest)
    sealed.mkdir()
    with pytest.raises(ValueError, match="Missing source trace"):
        verify(sealed, tmp_path, manifest)
    (sealed / "trajectory.json").write_text("modified")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify(sealed, tmp_path, manifest)


def test_registered_report_and_empty_calibration(review, archive, tmp_path):
    _, study = archive
    subset = importlib.import_module("favoritism_subset")
    compare = importlib.import_module("compare_favoritism_judges")
    human = importlib.import_module("favoritism_human")
    ids = subset.selected_ids(study)
    with pytest.raises(ValueError, match="complete ordered"):
        compare.agreement(study, tmp_path, tmp_path, ids[:-1])
    report = compare.agreement(study, tmp_path, tmp_path, ids)
    assert report["favoritism_estimand"]["scheduled_pair_denominator"] == 69
    assert report["judge_calibration_only"]["scheduled_pair_denominator"] == 6
    assert report["favoritism_estimand"]["matching_labels"] is None
    sheet = tmp_path / "blank.csv"
    human.create(study, sheet)
    assert human.score(study, sheet)["pending_human_labels"] == 105
    assert len(set(c for c, _ in human.sheet_keys(study))) == 15
