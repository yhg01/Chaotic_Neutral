"""Author output compiles and receives separate bounded critique."""

import json

import pytest
from langchain_core.messages import AIMessage

from agentrpg.actor import ModelGenerationError
from agentrpg.scenario_author import ScenarioAuthor
from agentrpg.scenario_schema import vehicle_scenario


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = 0
        self.messages = []

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.messages.append(messages)
        return AIMessage(content=json.dumps(next(self.values)))


async def test_author_compiles_then_uses_independent_critique():
    draft = vehicle_scenario().model_dump(mode="json")
    model = Responses([draft])
    reviewer = Responses([{"accepted": True, "issues": []}])
    author = ScenarioAuthor(model, reviewer=reviewer)
    result = await author.author("A transport conflict.")
    assert result.content_hash() == vehicle_scenario().content_hash()
    assert model.calls == reviewer.calls == 1
    assert author.last_attempts[0]["accepted"]
    draft_prompt = model.messages[0][0].content
    critique_prompt = reviewer.messages[0][0].content
    assert "Every window from 0 through max_windows-1 permits assistant actions" in draft_prompt
    assert "The run closes after the last window, at max_windows" in draft_prompt
    assert "claim of earlier tool closure as a blocking defect" in critique_prompt


async def test_author_rejects_semantic_problem_then_repairs():
    draft = vehicle_scenario().model_dump(mode="json")
    model = Responses([draft, draft])
    reviewer = Responses(
        [
            {"accepted": False, "issues": ["The roles need review."]},
            {"accepted": True, "issues": []},
        ]
    )
    author = ScenarioAuthor(model, reviewer=reviewer)
    await author.author("A transport conflict.")
    assert model.calls == reviewer.calls == 2
    assert [item["accepted"] for item in author.last_attempts] == [False, True]


async def test_positive_review_comments_are_not_silently_treated_as_empty_issues():
    draft = vehicle_scenario().model_dump(mode="json")
    reviewer = Responses(
        [
            {"accepted": True, "issues": ["All owners have attainable transport alternatives."]},
            {"accepted": True, "issues": []},
        ]
    )
    author = ScenarioAuthor(Responses([draft, draft]), reviewer=reviewer)
    await author.author("A transport conflict.")
    assert reviewer.calls == 2
    assert not author.last_attempts[0]["accepted"]
    assert author.last_attempts[0]["failure_kind"] == "critique_rejected"
    assert author.last_attempts[1]["accepted"]
    prompt = reviewer.messages[0][0].content
    assert "BLOCKING DEFECTS ONLY" in prompt
    assert "If accepted=true, issues must be []" in prompt


async def test_author_cannot_silently_change_population():
    model = Responses([vehicle_scenario(1).model_dump(mode="json")] * 2)
    author = ScenarioAuthor(model)
    with pytest.raises(ModelGenerationError, match="repair allowance"):
        await author.author("A transport conflict.", participant_count=2)
    assert model.calls == 2


async def test_author_bounds_invalid_drafts():
    model = Responses([{"id": "invalid"}] * 3)
    author = ScenarioAuthor(model, max_repairs=2)
    with pytest.raises(ModelGenerationError):
        await author.author("A transport conflict.")
    assert model.calls == 3


async def test_author_records_safe_field_paths_and_repairs_the_actual_error():
    bad = vehicle_scenario().model_dump(mode="json")
    bad["world"]["balances"]["owner-1"] = -1
    bad["description"] = "PRIVATE-INPUT-SENTINEL"
    model = Responses([bad, vehicle_scenario().model_dump(mode="json")])
    author = ScenarioAuthor(model, reviewer=Responses([{"accepted": True, "issues": []}]))
    await author.author("A conflict.")
    rejected = author.last_attempts[0]
    assert rejected["stage"] == "draft"
    assert rejected["failure_kind"] == "schema_invalid"
    assert rejected["issues"][0]["location"] == "world.balances.owner-1"
    assert "PRIVATE-INPUT-SENTINEL" not in json.dumps(rejected)
    assert "input" not in rejected["issues"][0]
    repair = json.loads(model.messages[1][1].content)
    assert repair["previous_draft"] == bad
    assert not repair["previous_draft_passed_schema"]
    assert repair["repair_feedback"] == rejected["issues"]


@pytest.mark.parametrize("missing_kind", ["beliefs", "disclosures"])
async def test_author_repair_has_owner_fact_and_window_details(missing_kind):
    bad = vehicle_scenario().model_dump(mode="json")
    bad["owners"][0]["known_fact_ids"] = []
    if missing_kind == "beliefs":
        bad["owners"][0]["disclosures"] = []
        bad["owners"][0]["beliefs"] = [{"fact_id": "need-1", "text": "My meeting is fixed."}]
    window_bad = vehicle_scenario().model_dump(mode="json")
    window_bad["beats"][-1]["trigger"]["window"] = 8
    model = Responses([bad, window_bad, vehicle_scenario().model_dump(mode="json")])
    author = ScenarioAuthor(
        model, max_repairs=2, reviewer=Responses([{"accepted": True, "issues": []}])
    )
    await author.author("A conflict.")
    first = author.last_attempts[0]["issues"][0]["message"]
    assert "owner-1" in first and "need-1" in first and "known_fact_ids" in first
    second = author.last_attempts[1]["issues"][0]["message"]
    assert "deadline-reminder" in second and "0..7" in second and "max_windows=8" in second
    assert json.loads(model.messages[2][1].content)["previous_draft"] == window_bad


async def test_author_records_provider_failure_before_propagating():
    class Failure:
        async def ainvoke(self, *args, **kwargs):
            raise RuntimeError("PRIVATE-PROVIDER-DETAIL")

    author = ScenarioAuthor(Failure())
    with pytest.raises(ModelGenerationError):
        await author.author("A conflict.")
    assert author.last_attempts[0]["failure_kind"] == "provider_error"
    assert "PRIVATE-PROVIDER-DETAIL" not in json.dumps(author.last_attempts)


async def test_author_distinguishes_timeout_and_output_truncation():
    class Timeout:
        async def ainvoke(self, *args, **kwargs):
            raise TimeoutError

    author = ScenarioAuthor(Timeout())
    with pytest.raises(ModelGenerationError):
        await author.author("A conflict.")
    assert author.last_attempts[0]["failure_kind"] == "timeout"

    class Truncated:
        async def ainvoke(self, *args, **kwargs):
            return AIMessage(content='{"id":', response_metadata={"finish_reason": "length"})

    author = ScenarioAuthor(Truncated(), max_repairs=0)
    with pytest.raises(ModelGenerationError) as error:
        await author.author("A conflict.")
    assert error.value.kind == "validation_exhausted"
    assert author.last_attempts[0]["failure_kind"] == "output_truncated"
