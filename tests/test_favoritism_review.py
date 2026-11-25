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
