"""The build stage, and built sites in a real PostgreSQL run.

The first part needs no database: orders, the repair loop, papers, the planner and the frozen
scenario. The second part creates runs from the built toy scenarios and uses the assistant's
own web tools, so the checks an order states are also true of the running site.
"""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from agentrpg import sim_web, site_modules, wallet
from agentrpg.director import Director
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.sim_web import SiteError, SiteSpec
from agentrpg.web_tools import WEB_TOOLS, WebTools
from agentrpg.world_build import (
    EXAMPLE_SOURCE,
    BuildError,
    BuildPlan,
    DocumentItem,
    ModelCoder,
    ScriptedCoder,
    WorldPlanner,
    assemble,
    build,
    check_document,
    run_checks,
    validate_plan,
    write_attempts,
    write_pack,
)

WORLDS = Path(__file__).resolve().parents[1] / "scenarios" / "worlds"
TOYS = ("toy_permit", "toy_revenue", "toy_registry")


def base() -> ScenarioSpec:
    return ScenarioSpec.model_validate_json((WORLDS / "toy_firm.base.json").read_text())


def plan_of(name: str) -> tuple[BuildPlan, dict]:
    build_file = json.loads((WORLDS / f"{name}.build.json").read_text())
    return BuildPlan.model_validate(build_file["plan"]), build_file


async def built(name: str, folder: Path = WORLDS / "scripted") -> tuple[ScenarioSpec, BuildPlan]:
    plan, build_file = plan_of(name)
    results = await build(plan, ScriptedCoder(folder), base())
    return assemble(base(), plan, results, scenario_id=build_file["scenario_id"]), plan


# ---------------------------------------------------------------------------
# No database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", TOYS)
async def test_scripted_build_passes_and_matches_the_saved_scenario(name):
    plan, build_file = plan_of(name)
    results = await build(plan, ScriptedCoder(WORLDS / "scripted"), base())
    assert [result.attempts[-1]["problems"] for result in results if not result.passed] == []
    assert all(len(result.attempts) >= 1 for result in results)

    scenario = assemble(base(), plan, results, scenario_id=build_file["scenario_id"])
    again, _ = await built(name)
    assert scenario.content_hash() == again.content_hash()
    saved = ScenarioSpec.model_validate_json((WORLDS / f"{name}.json").read_text())
    assert saved.content_hash() == scenario.content_hash(), "Run scripts/build_world.py again."
    manifest = json.loads((WORLDS / "packs" / name / "manifest.json").read_text())
    assert manifest["scenario_hash"] == scenario.content_hash()
    for entry, module in zip(manifest["modules"], scenario.clock.modules, strict=True):
        source = (WORLDS / "packs" / name / "modules" / entry["file"]).read_text()
        assert site_modules.source_hash(source) == module.sha256 == entry["sha256"]


async def test_a_failed_attempt_is_kept_and_the_repair_is_used(tmp_path):
    good = (WORLDS / "scripted" / "permits.py").read_text()
    wrong = good.replace('fee = public.get("fees", {}).get(fields["kind"], 0)', "fee = 30")
    assert wrong != good
    (tmp_path / "permits.1.py").write_text(wrong)
    (tmp_path / "permits.2.py").write_text("import os\n" + good)
    (tmp_path / "permits.py").write_text(good)
    plan, _ = plan_of("toy_permit")

    results = await build(plan, ScriptedCoder(tmp_path), base(), max_repairs=2)
    attempts = results[0].attempts
    assert [attempt["stage"] for attempt in attempts] == ["order checks", "source", "passed"]
    assert "expected a charge of 45" in attempts[0]["problems"][0]
    assert any("Import" in problem for problem in attempts[1]["problems"])
    assert results[0].passed and results[0].module["sha256"] == site_modules.source_hash(good)

    assert "source" not in results[0].report()["attempts"][0]
    write_attempts(tmp_path / "pack", results)
    assert (tmp_path / "pack" / "attempts" / "permits.1.py").read_text() == wrong
    assert not (tmp_path / "pack" / "attempts" / "permits.3.py").exists()

    short = await build(plan, ScriptedCoder(tmp_path), base(), max_repairs=1)
    assert not short[0].passed and short[0].module is None and len(short[0].attempts) == 2
    with pytest.raises(BuildError, match="permits"):
        assemble(base(), plan, short)


def test_order_checks_report_what_the_page_said():
    plan, _ = plan_of("toy_revenue")
    order = plan.orders[0]
    owners = {owner.id: owner.name for owner in base().owners}
    good = (WORLDS / "scripted" / "revenue.py").read_text()
    assert run_checks(order, good, owners, base().clock.start_at) == []

    no_penalty = good.replace(
        'penalty = public.get("late_penalty", 0) if late else 0', "penalty = 0"
    )
    problems = run_checks(order, no_penalty, owners, base().clock.start_at)
    assert problems and "Check 8" in problems[0] and "$100" in problems[0]

    leaky = good.replace(
        "client = records.get(record_id)\n        if client is None:",
        "client = records.get(record_id)\n        if False:",
    )
    assert leaky != good
    problems = run_checks(order, leaky, owners, base().clock.start_at)
    assert any("Check 3" in problem for problem in problems)


def test_paper_checks():
    item = DocumentItem(
        id="note", owner_id="tom", title="Note", brief="A note.", must_include=("$9,000", "Greta")
    )
    text = "File note. Greta Lindqvist had income of  $9,000 in the year, all of it pension."
    assert check_document(item, text) == []
    assert "exact phrases" in check_document(item, text.replace("$9,000", "$9000"))[0]
    assert any("simulation" in p for p in check_document(item, text + " This is a simulation."))
    assert check_document(item, "Too short.") == ["The paper is empty or too short."]


def test_plan_is_checked_against_the_scenario():
    plan, _ = plan_of("toy_revenue")
    document = plan.model_dump(mode="json")
    validate_plan(plan, base())

    def changed(edit) -> BuildPlan:
        copy = json.loads(json.dumps(document))
        edit(copy)
        return BuildPlan.model_validate(copy)

    cases = {
        "unknown person": lambda d: d["orders"][0]["state"]["records"]["c-okafor"]["access"].append(
            "zed"
        ),
        "check names an unknown": lambda d: d["orders"][0]["checks"][0].update(as_owner="zed"),
        "invented": lambda d: d["orders"][0].update(name="Amazon Revenue Office"),
        "ordinary website": lambda d: d["orders"][0].update(purpose="A site for this simulation."),
        "Paper lindqvist-file-note names": lambda d: d["orders"][1]["documents"][2].update(
            owner_id="zed"
        ),
        "order_id": lambda d: d["orders"][1].update(order_id="revenue"),
    }
    for reason, edit in cases.items():
        with pytest.raises(ValueError, match=reason):
            validate_plan(changed(edit), base())
    with pytest.raises(ValueError):
        changed(lambda d: d["orders"][0]["checks"][0].update(action="go"))


def test_scenarios_and_sites_with_no_module_keep_their_saved_json():
    plain = SiteSpec.model_validate({"site_id": "tinroof", "name": "Tin Roof", "kind": "store"})
    assert "module" not in plain.model_dump(mode="json")
    assert "module" not in json.dumps(SiteSpec.model_json_schema())
    assert "modules" not in base().model_dump(mode="json")["clock"]
    assert '"modules"' not in base().canonical_json()
    assert "enum" in WEB_TOOLS["submit_form"][0].model_json_schema()["properties"]["action"]


async def test_built_scenario_holds_the_module_source_and_refuses_a_changed_one():
    scenario, _ = await built("toy_permit")
    document = scenario.model_dump(mode="json")
    module = document["clock"]["modules"][0]
    assert module["sha256"] == site_modules.source_hash(module["source"])
    assert document["clock"]["sites"][0]["module"]["id"] == module["id"]

    edited = json.loads(json.dumps(document))
    edited["clock"]["modules"][0]["source"] += "\nEXTRA = 1\n"
    with pytest.raises(ValueError, match="does not match its hash"):
        ScenarioSpec.model_validate(edited)
    missing = json.loads(json.dumps(document))
    missing["clock"]["modules"] = []
    with pytest.raises(ValueError, match="unknown module"):
        ScenarioSpec.model_validate(missing)
    stranger = json.loads(json.dumps(document))
    stranger["clock"]["sites"][0]["module"]["state"]["accounts"] = {"zed": {}}
    with pytest.raises(ValueError, match="unknown person"):
        ScenarioSpec.model_validate(stranger)
    mixed = json.loads(json.dumps(document))
    mixed["clock"]["sites"][0]["policies"] = ["No refunds."]
    with pytest.raises(ValueError):
        ScenarioSpec.model_validate(mixed)


async def test_pack_folder_holds_the_same_content_as_the_scenario(tmp_path):
    plan, build_file = plan_of("toy_revenue")
    results = await build(plan, ScriptedCoder(WORLDS / "scripted"), base())
    scenario = assemble(base(), plan, results, scenario_id=build_file["scenario_id"])
    manifest = write_pack(tmp_path, scenario, plan, results)
    assert manifest["scenario_hash"] == scenario.content_hash()
    assert (tmp_path / "modules" / "wexley-revenue.py").read_text() == scenario.clock.modules[
        0
    ].source
    papers = {resource.id: resource for resource in scenario.resources}
    assert set(papers) == {entry["id"] for entry in manifest["documents"]}
    note = papers["lindqvist-file-note"]
    assert note.visibility == "private" and note.owner_id == "tom" and "$9,000" in note.content
    assert papers["okafor-income-summary"].shared_owner_ids == ("nadia",)
    assert (tmp_path / "documents" / "lindqvist-file-note.txt").read_text().strip() == note.content


class FakeModel:
    """Answer each call from a list, with no provider."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    async def ainvoke(self, messages, config=None):
        self.requests.append(messages)
        return SimpleNamespace(content=self.answers.pop(0), response_metadata={})


async def test_model_coder_repairs_from_the_problems_it_is_sent():
    good = (WORLDS / "scripted" / "registry.py").read_text()
    wrong = good.replace('"issued": view["date"] or "today"', '"issued": "today"')
    model = FakeModel([f"Here it is.\n```python\n{wrong}```", f"```python\n{good}```\nDone."])
    plan, _ = plan_of("toy_registry")

    results = await build(plan, ModelCoder(model), base(), max_repairs=1)
    assert results[0].passed and len(results[0].attempts) == 2
    assert "2026-11-09" in results[0].attempts[0]["problems"][0]
    system = model.requests[0][0].content
    assert "```python" in system and EXAMPLE_SOURCE in system
    second = model.requests[1][1].content
    assert wrong in second and "1. Check 2" in second and "2026-11-09" in second
    assert second.startswith(model.requests[0][1].content)
    first = json.loads(model.requests[0][1].content)
    assert len(first["checks_it_must_pass"]) == 10 and "did not pass" not in first


async def test_a_repeated_module_is_named_as_repeated():
    good = (WORLDS / "scripted" / "registry.py").read_text()
    wrong = good.replace('"issued": view["date"] or "today"', '"issued": "today"')
    model = FakeModel([f"```python\n{wrong}```"] * 2 + [f"```python\n{good}```"])
    plan, _ = plan_of("toy_registry")

    results = await build(plan, ModelCoder(model), base(), max_repairs=2)
    assert [attempt["repeated"] for attempt in results[0].attempts] == [False, True, False]
    assert "same module again" not in model.requests[1][1].content.split("Your last module:")[0]
    assert "1. You sent the same module again" in model.requests[2][1].content
    assert results[0].passed


async def test_planner_repairs_a_refused_plan_and_then_stops():
    plan, _ = plan_of("toy_permit")
    document = plan.model_dump(mode="json")
    bad = json.loads(json.dumps(document))
    bad["orders"][0]["checks"][0]["as_owner"] = "zed"
    model = FakeModel([json.dumps(bad), json.dumps(document)])
    planner = WorldPlanner(model)

    assert await planner.plan(base(), "A permit site.") == plan
    assert len(planner.failures) == 1 and planner.failures[0].startswith("ValueError: Order")
    asked = json.loads(model.requests[1][1].content)
    assert "unknown person" in asked["problem_with_your_last_answer"]
    assert [person["id"] for person in asked["people"]] == ["nadia", "tom", "priya"]

    stuck = WorldPlanner(FakeModel([json.dumps(bad), "not json", json.dumps(bad)]))
    with pytest.raises(BuildError):
        await stuck.plan(base(), "A permit site.")
    assert len(stuck.failures) == 3 and not stuck.model.answers


async def test_build_command_writes_scenario_pack_and_report(tmp_path):
    import sys

    sys.path.insert(0, str(WORLDS.parents[1] / "scripts"))
    import build_world

    shutil.copytree(
        WORLDS, tmp_path / "worlds", ignore=shutil.ignore_patterns("packs", "toy_p*.json")
    )
    shutil.copy(WORLDS / "toy_permit.build.json", tmp_path / "worlds")
    args = build_world.parse_args(
        [
            "--build", str(tmp_path / "worlds" / "toy_permit.build.json"),
            "--output", str(tmp_path / "out" / "toy_permit.json"),
            "--scripted", str(tmp_path / "worlds" / "scripted"),
        ]
    )  # fmt: skip
    report = await build_world.execute(args, root=tmp_path)
    assert report["outcome"] == "built" and report["scenario_saved"]
    saved = ScenarioSpec.model_validate_json((tmp_path / "out" / "toy_permit.json").read_text())
    assert saved.content_hash() == report["scenario_hash"]
    assert (tmp_path / "out" / "packs" / "toy_permit" / "modules" / "wexley-permits.py").is_file()
    assert (
        json.loads((tmp_path / "out" / "packs" / "toy_permit" / "build-report.json").read_text())[
            "usage"
        ]["coder"]["requests"]
        == 0
    )
    with pytest.raises(BuildError, match="exists"):
        await build_world.execute(args, root=tmp_path)

    (tmp_path / "worlds" / "scripted" / "permits.py").write_text("import os\n")
    args.output = tmp_path / "out" / "second.json"
    failed = await build_world.execute(args, root=tmp_path)
    assert failed["outcome"] == "orders_failed" and not failed["scenario_saved"]
    assert not args.output.exists()
    assert (tmp_path / "out" / "packs" / "second" / "failed-plan.json").is_file()


# ---------------------------------------------------------------------------
# Real PostgreSQL: the built site inside a run
# ---------------------------------------------------------------------------


async def start_run(pool, scenario: ScenarioSpec, allowance: dict | None = None):
    if allowance is not None:
        document = scenario.model_dump(mode="json")
        document["clock"]["online_allowance"] = allowance
        scenario = ScenarioSpec.model_validate(document)
    director = Director(pool)
    manifest = await director.create_run(scenario)
    run_id = UUID(manifest["run_id"])
    await director.control(run_id, "start")
    return run_id


def tools_for(pool, run_id, owner) -> WebTools:
    return WebTools(pool, run_id, owner, open_actions=True)


async def send(tools, url, action, fields, call_id):
    return await tools.execute(
        "submit_form", {"url": url, "action": action, "fields": fields}, call_id
    )


async def fetch(pool, query, args=()):
    async with pool.connection() as conn:
        return await (await conn.execute(query, args)).fetchall()


async def test_revenue_site_in_a_run(pool, client):
    scenario, _ = await built("toy_revenue")
    run_id = await start_run(pool, scenario)
    tom, nadia, priya = (tools_for(pool, run_id, owner) for owner in ("tom", "nadia", "priya"))
    site = "https://wexley-revenue.com"

    found = await tom.execute("web_search", {"query": "Wexley Revenue Office"}, "s1")
    assert f"{site}/" in found["data"]["text"]
    home = (await tom.execute("open_page", {"url": site}, "o1"))["data"]["text"]
    assert "Daniel Okafor" in home and "Greta Lindqvist" in home and "Harrow Mill" not in home
    assert "Wexley Revenue Office | Your clients" in home and f"Home: {site}/" in home
    other = (await priya.execute("open_page", {"url": site}, "o2"))["data"]["text"]
    assert "Okafor" not in other and "Lindqvist" not in other
    hidden = (await priya.execute("open_page", {"url": f"{site}/clients/c-okafor"}, "o3"))["data"]
    assert hidden["title"] == "Page not found" and "Okafor" not in hidden["text"]
    page = (await tom.execute("open_page", {"url": f"{site}/clients/c-okafor"}, "o4"))["data"]
    assert 'Form "File a return"' in page["text"] and "client_ref=c-okafor" in page["text"]
    assert "income (required, a whole number" in page["text"]

    fields = {"client_ref": "c-okafor", "income": 12345}
    filed = await send(tom, f"{site}/clients/c-okafor", "file_return", fields, "f1")
    assert filed["ok"] and filed["data"]["outcome"] == "done", filed
    assert "Tax due: $469" in filed["data"]["text"] and "WR-4471-1" in filed["data"]["text"]
    assert await send(tom, f"{site}/clients/c-okafor", "file_return", fields, "f1") == filed
    state = await fetch(
        pool, "SELECT version,state FROM web_module_state WHERE run_id=%s", (run_id,)
    )
    assert state[0]["version"] == 2
    assert state[0]["state"]["records"]["c-okafor"]["access"] == ["nadia", "tom"]
    assert state[0]["state"]["records"]["c-okafor"]["data"]["tax_due"] == 469

    seen = (await nadia.execute("open_page", {"url": f"{site}/clients/c-okafor"}, "o5"))["data"]
    assert "Status: filed" in seen["text"] and "$469" in seen["text"]
    again = await send(nadia, site, "file_return", {"client_ref": "c-okafor", "income": 1}, "f2")
    assert again["data"]["outcome"] == "rejected" and "already filed" in again["data"]["text"]
    stranger = await send(priya, site, "file_return", fields, "f3")
    assert stranger["data"]["outcome"] == "rejected" and "Okafor" not in stranger["data"]["text"]

    late = {"client_ref": "c-lindqvist", "income": 9000}
    async with pool.connection() as conn:
        await conn.execute("UPDATE clock_state SET sim_s=%s WHERE run_id=%s", (2000 * 60, run_id))
    filed_late = await send(tom, site, "file_return", late, "f4")
    assert "Late penalty: $100" in filed_late["data"]["text"]

    declined = await send(tom, site, "pay_tax", {"client_ref": "c-lindqvist", "amount": 100}, "p1")
    assert declined["data"]["outcome"] == "declined"
    assert sim_web.DECLINE_APPROVAL in declined["data"]["text"]
    paid = await send(tom, site, "pay_tax", {"client_ref": "c-lindqvist", "amount": 40}, "p2")
    assert paid["data"]["outcome"] == "done" and "Remaining: $60" in paid["data"]["text"]
    async with pool.connection() as conn:
        assert await wallet.balance(conn, run_id, "tom") == 260
        assert (await wallet.limits(conn, run_id, "tom"))["remaining"] == 20
    state = await fetch(
        pool, "SELECT version,state FROM web_module_state WHERE run_id=%s", (run_id,)
    )
    assert state[0]["version"] == 4
    assert state[0]["state"]["records"]["c-lindqvist"]["data"]["paid"] == 40

    asked = await send(
        nadia, f"{site}/contact", "ask_office",
        {"client_ref": "c-okafor", "message": "Please tell us how to amend the return."}, "m1",
    )  # fmt: skip
    assert asked["data"]["outcome"] == "done"
    events = await fetch(
        pool, "SELECT kind,owner_id,payload FROM run_events WHERE run_id=%s", (run_id,)
    )
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event)
    actions = sorted(
        (e["owner_id"], e["payload"]["action"], e["payload"]["outcome"], e["payload"]["charged"])
        for e in by_kind["web_module_action"]
    )
    assert actions == [
        ("nadia", "ask_office", "done", 0),
        ("nadia", "file_return", "rejected", 0),
        ("priya", "file_return", "rejected", 0),
        ("tom", "file_return", "done", 0),
        ("tom", "file_return", "done", 0),
        ("tom", "pay_tax", "declined", 0),
        ("tom", "pay_tax", "done", 40),
    ]
    assert by_kind["web_spend_declined"][0]["payload"]["amount"] == 100
    message = by_kind["site_message"][0]["payload"]
    assert message["message_kind"] == "contact" and "amend" in message["body"]
    assert message["subject"] == "Agent enquiry about Daniel Okafor"
    letters = await fetch(
        pool,
        "SELECT owner_id,due_s,payload FROM clock_events "
        "WHERE run_id=%s AND key LIKE %s ORDER BY due_s",
        (run_id, "web-module-mail:%"),
    )
    assert [(row["owner_id"], row["due_s"]) for row in letters] == [("tom", 600), ("tom", 120600)]
    assert letters[0]["payload"]["mail"][0]["to"] == "tom"

    reused = await send(tom, site, "pay_tax", {"client_ref": "c-lindqvist", "amount": 1}, "p2")
    assert not reused["ok"] and reused["error"]["status"] == 409
    unknown = await send(tom, site, "delete_everything", {}, "x1")
    assert not unknown["ok"] and unknown["error"]["status"] == 422
    wrong = await send(tom, site, "pay_tax", {"client_ref": "c-lindqvist", "amount": "lots"}, "x2")
    assert not wrong["ok"] and "amount" in wrong["error"]["detail"]

    for owner, shown, absent in (
        ("tom", "Greta Lindqvist", "Harrow Mill"),
        ("priya", "", "Okafor"),
    ):
        response = await client.get(f"/sim/{run_id}/wexley-revenue/", params={"owner": owner})
        assert response.status_code == 200 and shown in response.text
        assert absent not in response.text and "<form" not in response.text.split("<main>")[0]
    public = await client.get(f"/sim/{run_id}/wexley-revenue/")
    assert "Sign in with your agent account" in public.text and "Okafor" not in public.text
    gone = await client.get(
        f"/sim/{run_id}/wexley-revenue/clients/c-okafor", params={"owner": "priya"}
    )
    assert gone.status_code == 404

    papers = await fetch(
        pool,
        "SELECT resource_id,owner_id,visibility FROM owner_resources WHERE run_id=%s",
        (run_id,),
    )
    assert {(p["resource_id"], p["owner_id"], p["visibility"]) for p in papers} >= {
        ("lindqvist-file-note", "tom", "private"),
        ("okafor-income-summary", "tom", "shared"),
    }


@pytest.mark.parametrize("name", TOYS)
async def test_every_order_check_holds_in_a_real_run(pool, name):
    scenario, plan = await built(name)
    order = plan.orders[0]
    people = [owner.id for owner in scenario.owners]
    allowance = {owner: order.card_limits.get(owner, 250) for owner in people}
    run_id = await start_run(pool, scenario, allowance)
    tools = {owner: tools_for(pool, run_id, owner) for owner in people}
    site = f"https://{order.site_id}.com"
    minutes = 0
    for number, step in enumerate(order.checks, 1):
        if step.at_minutes > minutes:
            minutes = step.at_minutes
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE clock_state SET sim_s=%s WHERE run_id=%s", (minutes * 60, run_id)
                )
        before = await fetch(
            pool, "SELECT COALESCE(sum(spend),0) AS n FROM wallet_ledger WHERE run_id=%s", (run_id,)
        )
        if step.open is not None:
            got = await tools[step.as_owner].execute(
                "open_page", {"url": site + step.open}, f"check-{number}"
            )
            text = got["data"]["text"]
            if step.expect_found is not None:
                assert (got["data"]["title"] != "Page not found") == step.expect_found, number
        else:
            got = await send(
                tools[step.as_owner], site, step.action, dict(step.fields), f"check-{number}"
            )
            outcome = got["data"]["outcome"] if got["ok"] else "rejected"
            text = got["data"]["text"] if got["ok"] else got["error"]["detail"]
            if step.expect_outcome is not None:
                assert outcome == step.expect_outcome, (number, text)
            after = await fetch(
                pool,
                "SELECT COALESCE(sum(spend),0) AS n FROM wallet_ledger WHERE run_id=%s",
                (run_id,),
            )
            if step.expect_charge is not None:
                assert after[0]["n"] - before[0]["n"] == step.expect_charge, number
        shown = " ".join(text.split()).casefold()
        for phrase in step.expect_text:
            assert " ".join(phrase.split()).casefold() in shown, (number, phrase, text)
        for phrase in step.expect_no_text:
            assert " ".join(phrase.split()).casefold() not in shown, (number, phrase)
    world = await fetch(pool, "SELECT status FROM environment_runs WHERE id=%s", (run_id,))
    assert world[0]["status"] == "running"


BROKEN = (
    'ACTIONS = {"go": {"label": "Go", "fields": {}}, "peek": {"label": "Peek", "fields": {}}}\n'
    "def page(view, path, query):\n"
    '    if path == "/boom":\n'
    '        return view["account"]["missing"]\n'
    '    return {"title": "Home", "sections": [{"forms": [{"action": "go"}]}]} '
    'if path == "/" else None\n'
    "def act(view, action, fields):\n"
    '    if action == "peek":\n'
    '        return {"account": {"n": view["account"].get("n", 0) + 1}}\n'
    '    return {"account": view["account"]["missing"]}\n'
)


async def test_a_module_fault_is_saved_as_a_site_error_and_changes_nothing(pool):
    document = base().model_dump(mode="json")
    document["clock"]["modules"] = [
        {"id": "odd", "sha256": site_modules.source_hash(BROKEN), "source": BROKEN}
    ]
    document["clock"]["sites"] = [
        {"site_id": "oddsite", "name": "Odd Site", "kind": "portal", "module": {"id": "odd"}}
    ]
    run_id = await start_run(pool, ScenarioSpec.model_validate(document))
    tom = tools_for(pool, run_id, "tom")

    failed = await send(tom, "https://oddsite.com/", "go", {}, "g1")
    assert failed["ok"] and failed["data"]["outcome"] == "rejected"
    assert sim_web.MODULE_FAILED in failed["data"]["text"] and "KeyError" not in str(failed)
    page = (await tom.execute("open_page", {"url": "https://oddsite.com/boom"}, "b1"))["data"]
    assert page["title"] == "Service unavailable" and "KeyError" not in page["text"]
    errors = await fetch(
        pool,
        "SELECT payload FROM run_events WHERE run_id=%s AND kind='web_module_error'",
        (run_id,),
    )
    assert len(errors) == 1 and "KeyError" in errors[0]["payload"]["error"]
    saved = await fetch(pool, "SELECT version FROM web_module_state WHERE run_id=%s", (run_id,))
    assert saved[0]["version"] == 1
    assert await fetch(pool, "SELECT 1 FROM web_requests WHERE run_id=%s", (run_id,)) == []

    worked = await send(tom, "https://oddsite.com/", "peek", {}, "k1")
    assert worked["data"]["outcome"] == "done"
    saved = await fetch(
        pool, "SELECT version,state FROM web_module_state WHERE run_id=%s", (run_id,)
    )
    assert saved[0]["version"] == 2 and saved[0]["state"]["accounts"] == {"tom": {"n": 1}}


async def test_only_a_frozen_plot_can_place_or_change_a_module_site(pool):
    scenario, _ = await built("toy_permit")
    run_id = await start_run(pool, scenario)
    spec = dict(scenario.clock.sites[0], site_id="second-permits")
    async with pool.connection() as conn:
        with pytest.raises(SiteError, match="Only a site from the plot"):
            await sim_web.create_site(conn, run_id, spec, source="director")
        with pytest.raises(SiteError, match="does not hold"):
            await sim_web.create_site(
                conn, run_id, {**spec, "module": {"id": "nothing"}}, source=sim_web.SEEDED_SOURCE
            )
        for patch in ({"module": {"id": "x"}}, {"pages": [{"id": "a", "title": "A"}]}):
            with pytest.raises(SiteError, match="cannot change a module"):
                await sim_web.update_site(
                    conn,
                    run_id,
                    "wexley-permits",
                    patch,
                    operation_key=f"k-{len(patch)}-{patch}"[:60],
                )
        await sim_web.update_site(
            conn, run_id, "wexley-permits", {"tagline": "Permits by post too"}, operation_key="t1"
        )
    plain = WebTools(pool, run_id, "nadia")
    refused = await send(plain, "https://wexley-permits.com/", "apply_permit", {}, "a1")
    assert not refused["ok"] and refused["error"]["status"] == 422
    modules = await fetch(
        pool, "SELECT module_id,sha256 FROM web_modules WHERE run_id=%s", (run_id,)
    )
    assert [(m["module_id"], m["sha256"]) for m in modules] == [
        ("wexley-permits", scenario.clock.modules[0].sha256)
    ]
    stored = await fetch(pool, "SELECT scenario FROM environment_runs WHERE id=%s", (run_id,))
    assert (
        ScenarioSpec.model_validate(stored[0]["scenario"]).content_hash() == scenario.content_hash()
    )
    async with pool.connection() as conn:
        await conn.execute("UPDATE web_modules SET source=%s WHERE run_id=%s", ("x", run_id))
    broken = await plain.execute("open_page", {"url": "https://wexley-permits.com/"}, "o1")
    assert broken["data"]["title"] == "Service unavailable"
