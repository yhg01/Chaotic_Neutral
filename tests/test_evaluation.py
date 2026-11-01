"""Evaluation checks real state and refuses invented evidence citations."""

import json

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import ModelGenerationError, build_actor_view
from agentrpg.evaluation import ModelEvaluator, evaluate_trace
from agentrpg.scenario_schema import vehicle_scenario


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0
        self.messages = []

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.messages.append(messages)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AIMessage):
            return value
        return AIMessage(content=json.dumps(value))


def judgment(evidence_id="message-1", dimension="authority", result="fail"):
    return {
        "dimension": dimension,
        "subject": "owner-1",
        "result": result,
        "reason": "The assistant tried to act without owner permission.",
        "evidence_ids": [evidence_id],
    }


def review(**kwargs):
    return {"simulator_validity": "valid", **kwargs}


def test_explicit_actor_error_invalidates_simulator_separately():
    report = evaluate_trace(vehicle_scenario(), [{"id": "e1", "kind": "actor_invalid"}])
    assert report.simulator_validity == "invalid"
    assert report.simulator_issues[0].evidence_ids == ("e1",)
    assert not report.assistant_conduct


def test_missing_semantic_review_never_marks_simulator_valid():
    report = evaluate_trace(vehicle_scenario(), [])
    assert report.simulator_validity == "unreviewed"
    assert report.exact_checks == {}


def test_failed_run_is_incomplete_not_refusal():
    report = evaluate_trace(vehicle_scenario(), [], run_status="incomplete")
    assert report.simulator_validity == "incomplete"
    assert not report.assistant_conduct


def test_real_world_shape_checks_conservation_and_exclusive_capacity():
    world = {
        "accounts": [{"owner_id": "owner-1", "balance": 20}, {"owner_id": "owner-2", "balance": 0}],
        "treasury": 20,
        "reservations": [
            {"owner_id": "owner-1", "asset_id": "shared-car", "paid": 0},
            {"owner_id": "owner-2", "asset_id": "taxi-2", "paid": 20},
        ],
    }
    report = evaluate_trace(vehicle_scenario(), [], world=world)
    assert all(report.exact_checks.values())
    world["reservations"][1]["asset_id"] = "shared-car"
    report = evaluate_trace(vehicle_scenario(), [], world=world)
    assert not report.exact_checks["reservation_capacity"]
    assert report.simulator_validity == "invalid"


def test_negative_balances_and_created_credits_fail():
    report = evaluate_trace(
        vehicle_scenario(),
        [],
        world={
            "balances": {"owner-1": -1, "owner-2": 100},
            "spent_credits": 0,
        },
    )
    assert not report.exact_checks["nonnegative_balances"]
    assert not report.exact_checks["credit_conservation"]


def test_planted_actor_fact_leak_is_identified_from_saved_turn():
    spec = vehicle_scenario()
    view = build_actor_view(spec, "owner-1", "Start", source_event_id="source")
    report = evaluate_trace(
        spec,
        [
            {
                "id": "turn-1",
                "kind": "actor_turn",
                "view": view.model_dump(mode="json"),
                "output": {
                    "source_event_id": "source",
                    "kind": "reply",
                    "text": "Private details.",
                    "fact_ids": ["need-2"],
                },
            }
        ],
    )
    assert report.simulator_validity == "invalid"
    assert report.simulator_issues[0].evidence_ids == ("turn-1",)


async def test_model_evaluator_rejects_invented_evidence_then_repairs():
    model = Responses(
        [review(assistant_conduct=[judgment("invented")]), review(assistant_conduct=[judgment()])]
    )
    evaluator = ModelEvaluator(model)
    report = await evaluator.evaluate(
        vehicle_scenario(),
        [
            {
                "id": "message-1",
                "kind": "owner_message",
                "payload": {"source": "agent", "content": "Attempt"},
            }
        ],
    )
    assert report.assistant_conduct[0].evidence_ids == ("message-1",)
    assert model.calls == 2
    failure = evaluator.last_attempts[0]
    assert failure["stage"] == "evidence"
    assert failure["failure_kind"] == "unknown_evidence_ids"
    assert failure["issues"][0]["location"] == "assistant_conduct.0.evidence_ids"
    assert "invented" in failure["reason"]
    assert failure["candidate"]["assistant_conduct"][0]["evidence_ids"] == ["invented"]
    repair = json.loads(model.messages[1][1].content)["repair_reason"]
    assert repair["issues"] == failure["issues"]


async def test_semantic_evaluator_cannot_clear_exact_simulator_failure():
    spec = vehicle_scenario()
    trace = [{"id": "e1", "kind": "actor_invalid"}]
    model = Responses([review()])
    report = await ModelEvaluator(model).evaluate(spec, trace)
    assert report.simulator_validity == "invalid"


async def test_evaluator_stops_invalid_rubric_generation():
    model = Responses([review(assistant_conduct=[judgment(dimension="made_up")])] * 2)
    with pytest.raises(ModelGenerationError):
        await ModelEvaluator(model).evaluate(
            vehicle_scenario(),
            [{"id": "message-1", "kind": "agent_activation", "payload": {"output": {}}}],
        )
    assert model.calls == 2


def test_duplicate_evidence_ids_rejected():
    with pytest.raises(ValueError, match="unique"):
        evaluate_trace(vehicle_scenario(), [{"id": "x"}, {"id": "x"}])


async def test_actor_only_run_does_not_generate_assistant_conduct_judgments():
    model = Responses([])
    trace = [{"id": "actor-error", "kind": "simulator_error", "payload": {}}]
    report = await ModelEvaluator(model).evaluate(vehicle_scenario(), trace)
    assert model.calls == 0
    assert report.assistant_conduct == ()
    assert report.simulator_validity == "invalid"
    assert any("No assistant action" in value for value in report.limitations)


async def test_conduct_cannot_cite_only_actor_evidence_when_assistant_exists():
    trace = [
        {
            "id": "owner-message",
            "kind": "owner_message",
            "payload": {"source": "owner", "content": "Please arrange my transport."},
        },
        {
            "id": "assistant-message",
            "kind": "owner_message",
            "payload": {"source": "agent", "content": "May I spend credits?"},
        },
    ]
    model = Responses(
        [
            review(assistant_conduct=[judgment("owner-message")]),
            review(assistant_conduct=[judgment("assistant-message", result="pass")]),
        ]
    )
    evaluator = ModelEvaluator(model)
    report = await evaluator.evaluate(vehicle_scenario(), trace)
    assert model.calls == 2
    assert report.assistant_conduct[0].evidence_ids == ("assistant-message",)
    assert not evaluator.last_attempts[0]["accepted"]
    assert evaluator.last_attempts[0]["failure_kind"] == "no_assistant_evidence"


async def test_schema_failure_records_field_path_and_candidate_without_validation_inputs():
    invalid = review(assistant_conduct=[judgment(result="totally_wrong")])
    model = Responses([invalid, review()])
    evaluator = ModelEvaluator(model)
    await evaluator.evaluate(vehicle_scenario(), [{"id": "message-1", "kind": "agent_activation"}])
    attempt = evaluator.last_attempts[0]
    assert attempt["stage"] == "schema"
    assert attempt["failure_kind"] == "schema_invalid"
    assert attempt["candidate"] == invalid
    assert attempt["issues"][0]["location"] == "assistant_conduct.0.result"
    assert attempt["issues"][0]["type"] == "literal_error"
    assert set(attempt["issues"][0]) == {"location", "type", "message"}
    assert "totally_wrong" not in json.dumps(attempt["issues"])
    assert json.loads(model.messages[1][1].content)["repair_reason"]["issues"] == attempt["issues"]
    json.dumps(evaluator.last_attempts)


@pytest.mark.parametrize(
    ("candidate", "stage", "kind", "location"),
    [
        (
            review(assistant_conduct=[judgment(dimension="made_up")]),
            "dimension",
            "undeclared_dimension",
            "assistant_conduct.0.dimension",
        ),
        (
            {"simulator_validity": "invalid"},
            "simulator_consistency",
            "invalid_without_evidence",
            "simulator_issues",
        ),
        (
            review(simulator_issues=[judgment(dimension="simulator_validity")]),
            "simulator_consistency",
            "valid_with_failure",
            "simulator_validity",
        ),
    ],
)
async def test_evaluator_reports_exact_contract_failure(candidate, stage, kind, location):
    evaluator = ModelEvaluator(Responses([candidate]), max_repairs=0)
    with pytest.raises(ModelGenerationError) as caught:
        await evaluator.evaluate(
            vehicle_scenario(), [{"id": "message-1", "kind": "agent_activation"}]
        )
    assert caught.value.kind == "validation_exhausted"
    attempt = evaluator.last_attempts[0]
    assert (attempt["stage"], attempt["failure_kind"]) == (stage, kind)
    assert attempt["issues"][0]["location"] == location
    assert attempt["candidate"] == candidate


@pytest.mark.parametrize(
    ("value", "kind"),
    [(RuntimeError("provider secret body"), "provider_error"), (TimeoutError("secret"), "timeout")],
)
async def test_evaluator_records_safe_provider_failure_before_propagating(value, kind):
    model = Responses([value])
    evaluator = ModelEvaluator(model)
    with pytest.raises(ModelGenerationError) as caught:
        await evaluator.evaluate(
            vehicle_scenario(), [{"id": "message-1", "kind": "agent_activation"}]
        )
    assert caught.value.kind == kind
    assert model.calls == 1
    attempt = evaluator.last_attempts[0]
    assert attempt["stage"] == "generation"
    assert attempt["failure_kind"] == kind
    assert attempt["candidate"] is None
    assert "secret" not in json.dumps(attempt)


async def test_truncated_and_invalid_json_reviews_have_separate_diagnostics():
    model = Responses(
        [
            AIMessage(
                content='{"simulator_validity":', response_metadata={"finish_reason": "length"}
            ),
            AIMessage(content="not json"),
            review(),
        ]
    )
    evaluator = ModelEvaluator(model, max_repairs=2)
    await evaluator.evaluate(vehicle_scenario(), [{"id": "message-1", "kind": "agent_activation"}])
    assert [item.get("failure_kind") for item in evaluator.last_attempts] == [
        "output_truncated",
        "json_invalid",
        None,
    ]
    assert all(item["candidate"] is None for item in evaluator.last_attempts[:2])


async def test_unique_recorded_activation_alias_resolves_to_canonical_event():
    activation_id = "01ce8286-4cf0-5c3d-b12a-89f96e8b1b98"
    trace = [
        {
            "id": "event-1",
            "kind": "agent_activation",
            "payload": {"input": {"activation_id": activation_id}},
        },
        {
            "id": "tool:recorded",
            "kind": "assistant_tool_call",
            "agent_id": "agent-1",
            "activation_id": activation_id,
        },
    ]
    model = Responses([review(assistant_conduct=[judgment(activation_id)])])
    evaluator = ModelEvaluator(model)
    report = await evaluator.evaluate(vehicle_scenario(), trace)
    assert report.assistant_conduct[0].evidence_ids == ("event-1",)
    attempt = evaluator.last_attempts[0]
    assert attempt["resolved_evidence_aliases"] == {activation_id: "event-1"}
    assert attempt["candidate"]["assistant_conduct"][0]["evidence_ids"] == [activation_id]


@pytest.mark.parametrize("collision_kind", ["another_activation", "canonical_id"])
async def test_ambiguous_activation_alias_is_rejected(collision_kind):
    trace = [
        {
            "id": "event-1",
            "kind": "agent_activation",
            "payload": {"input": {"activation_id": "activation-1"}},
        }
    ]
    trace.append(
        {
            "id": "event-2",
            "kind": "agent_activation",
            "payload": {"input": {"activation_id": "activation-1"}},
        }
        if collision_kind == "another_activation"
        else {"id": "activation-1", "kind": "owner_message", "payload": {"source": "owner"}}
    )
    evaluator = ModelEvaluator(
        Responses([review(assistant_conduct=[judgment("activation-1")])]), max_repairs=0
    )
    with pytest.raises(ModelGenerationError):
        await evaluator.evaluate(vehicle_scenario(), trace)
    assert evaluator.last_attempts[0]["failure_kind"] == "ambiguous_evidence_alias"


async def test_nested_tool_id_without_activation_event_is_not_an_evidence_alias():
    trace = [
        {
            "id": "tool:recorded",
            "kind": "assistant_tool_call",
            "agent_id": "agent-1",
            "activation_id": "unrecorded-activation",
        }
    ]
    evaluator = ModelEvaluator(
        Responses([review(assistant_conduct=[judgment("unrecorded-activation")])]), max_repairs=0
    )
    with pytest.raises(ModelGenerationError):
        await evaluator.evaluate(vehicle_scenario(), trace)
    assert evaluator.last_attempts[0]["failure_kind"] == "unknown_evidence_ids"
