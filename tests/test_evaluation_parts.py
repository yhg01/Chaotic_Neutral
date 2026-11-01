"""Large reviews retain owner evidence and use public peer context."""

import json

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import ModelGenerationError
from agentrpg.evaluation import (
    ModelEvaluator,
    ReviewContractError,
    SemanticReview,
    _activation_aliases,
    _resolve_activation_citations,
    evaluate_trace,
)
from agentrpg.scenario_schema import vehicle_scenario


class Reviewer:
    def __init__(self, *, wrong_subject=False, fail_second=False, board_only=False):
        self.inputs = []
        self.wrong_subject = wrong_subject
        self.fail_second = fail_second
        self.board_only = board_only

    async def ainvoke(self, messages, config=None):
        data = json.loads(messages[-1].content)
        self.inputs.append(data)
        owner = data["focus_owner_id"]
        if self.fail_second and owner == "owner-2":
            raise RuntimeError("Mocked provider failure")
        return AIMessage(
            content=json.dumps(
                {
                    "simulator_validity": "valid",
                    "assistant_conduct": [
                        {
                            "dimension": data["allowed_assistant_dimensions"][0],
                            "subject": "wrong-owner" if self.wrong_subject else owner,
                            "result": "pass",
                            "reason": "The assistant made an observed request.",
                            "evidence_ids": [
                                f"board-{owner}" if self.board_only else f"action-{owner}"
                            ],
                        }
                    ],
                }
            )
        )


def evidence():
    return [
        *[
            {
                "id": f"action-owner-{number}",
                "kind": "agent_activation",
                "owner_id": f"owner-{number}",
                "payload": {
                    "input": {"private": str(number) * 105_000},
                    "output": {"text": "Request"},
                },
            }
            for number in (1, 2)
        ],
        {
            "id": "peer-post",
            "kind": "board_message",
            "owner_id": "owner-2",
            "agent_id": "agent-2",
            "content": "Public offer",
        },
        {"id": "clock-1", "kind": "window_reached", "payload": {"window": 1}},
    ]


async def test_owner_parts_preserve_all_private_records_and_public_context():
    spec, trace, model = vehicle_scenario(), evidence(), Reviewer()
    evaluator = ModelEvaluator(model)
    report = await evaluator.evaluate(spec, trace)
    assert report.simulator_validity == "valid"
    assert {item.subject for item in report.assistant_conduct} == {"owner-1", "owner-2"}
    assert len(model.inputs) == 2
    for data in model.inputs:
        own = data["focus_owner_id"]
        records = {item["id"]: item for item in data["trace"]}
        assert set(records) == {f"action-{own}", "peer-post", "clock-1"}
        assert len(records[f"action-{own}"]["payload"]["input"]["private"]) == 105_000
    assert [item["focus_owner_id"] for item in evaluator.last_attempts] == ["owner-1", "owner-2"]
    assert trace == evidence()
    assert "separate owner parts" in report.limitations[-1]


async def test_owner_parts_preserve_exact_invalid_baseline():
    spec, trace = vehicle_scenario(), evidence()
    baseline = evaluate_trace(spec, trace, run_status="invalid")
    report = await ModelEvaluator(Reviewer()).evaluate(spec, trace, baseline=baseline)
    assert report.simulator_validity == "invalid"


async def test_owner_part_cannot_judge_another_owner():
    with pytest.raises(ModelGenerationError, match="exhausted"):
        await ModelEvaluator(Reviewer(wrong_subject=True), max_repairs=0).evaluate(
            vehicle_scenario(), evidence()
        )


async def test_failed_later_part_keeps_earlier_review_diagnostics():
    evaluator = ModelEvaluator(Reviewer(fail_second=True), max_repairs=0)
    with pytest.raises(ModelGenerationError):
        await evaluator.evaluate(vehicle_scenario(), evidence())
    assert len(evaluator.last_attempts) == 2
    assert evaluator.last_attempts[0]["accepted"] is True
    assert evaluator.last_attempts[1]["accepted"] is False
    assert evaluator.last_attempts[1]["focus_owner_id"] == "owner-2"


def test_missing_tool_prefix_resolves_only_a_unique_saved_evidence_id():
    aliases, ambiguous = _activation_aliases(
        {
            "tool:abc": {"kind": "assistant_tool_call"},
            "board:abc": {"kind": "board_message"},
            "tool:unique": {"kind": "assistant_tool_call"},
        }
    )
    review = SemanticReview.model_validate(
        {
            "simulator_validity": "valid",
            "assistant_conduct": [
                {
                    "dimension": "honesty",
                    "subject": "owner-1",
                    "result": "pass",
                    "reason": "The record supports the claim.",
                    "evidence_ids": ["unique"],
                }
            ],
        }
    )
    resolved, mapping = _resolve_activation_citations(review, aliases, ambiguous)
    assert resolved.assistant_conduct[0].evidence_ids == ("tool:unique",)
    assert mapping == {"unique": "tool:unique"}
    conflict = review.model_copy(
        update={
            "assistant_conduct": (
                review.assistant_conduct[0].model_copy(update={"evidence_ids": ("abc",)}),
            )
        }
    )
    with pytest.raises(ReviewContractError, match="multiple evidence records"):
        _resolve_activation_citations(conflict, aliases, ambiguous)


async def test_part_recognizes_own_board_author_without_owner_field():
    trace = evidence()
    for number in (1, 2):
        trace.extend(
            [
                {
                    "id": f"tool-owner-{number}",
                    "kind": "assistant_tool_call",
                    "owner_id": f"owner-{number}",
                    "agent_id": f"agent-{number}",
                },
                {
                    "id": f"board-owner-{number}",
                    "kind": "board_message",
                    "author_id": f"agent-{number}",
                    "content": "A public offer",
                },
            ]
        )
    model = Reviewer(board_only=True)
    report = await ModelEvaluator(model).evaluate(vehicle_scenario(), trace)
    assert report.simulator_validity == "valid"
    for part in model.inputs:
        owner = part["focus_owner_id"]
        peer = "owner-1" if owner == "owner-2" else "owner-2"
        assert f"board-{owner}" in part["assistant_evidence_ids"]
        assert f"board-{peer}" not in part["assistant_evidence_ids"]
