"""Checks of built site modules with no database: source rules, limits, privacy and results."""

import pytest

from agentrpg import site_modules
from agentrpg.site_modules import (
    FieldProblem,
    ModuleProblem,
    ModuleRunError,
    check_module,
    check_source,
    clean_fields,
    load,
    render_page,
    run_action,
    view_for,
)
from agentrpg.world_build import EXAMPLE_SOURCE

OWNERS = {"dan", "mei"}
LIBRARY_STATE = {
    "public": {"renewal_fee": 4, "titles": ["A Wexley Year", "Joinery by Hand"]},
    "accounts": {"dan": {"card_number": "L-2001", "valid_until": "2026-12-31"}},
}
HEAD = 'ACTIONS = {"go": {"label": "Go", "fields": {"n": {"type": "int"}}}}\n'
PAGE = 'def page(view, path, query):\n    return {"title": "Home"} if path == "/" else None\n'


def module(act_body: str, actions: str = HEAD) -> str:
    return actions + PAGE + "def act(view, action, fields):\n" + act_body


def state(**parts):
    return site_modules.check_state(parts)


def test_example_in_the_coder_guide_passes_every_fixed_check():
    assert check_module(EXAMPLE_SOURCE, LIBRARY_STATE, OWNERS) == []


def test_example_site_charges_and_keeps_each_account_apart():
    loaded = load(EXAMPLE_SOURCE)
    start = site_modules.check_state(LIBRARY_STATE, OWNERS)
    context = {"date": "2026-11-09", "viewer_name": "Dan"}

    result = run_action(loaded, start, "dan", context, "renew_card", {"years": "2"}, strict=True)
    assert result.outcome == "done" and result.charge == {
        "amount": 8,
        "memo": "Card renewal, 2 year(s)",
    }
    assert result.state["accounts"]["dan"]["valid_until"] == "2028-12-31"
    assert [letter["in_minutes"] for letter in result.mail] == [5]
    assert start["accounts"]["dan"]["valid_until"] == "2026-12-31"

    refused = run_action(loaded, start, "mei", context, "renew_card", {"years": "1"}, strict=True)
    assert refused.outcome == "rejected" and refused.charge is None and not refused.changed

    found = run_action(loaded, start, "mei", context, "find_title", {"q": "joinery"}, strict=True)
    listing = render_page(loaded, start, "mei", context, found.path, found.query)
    assert listing["sections"][0]["lines"] == ["- Joinery by Hand"]
    assert "L-2001" not in site_modules.page_text(render_page(loaded, start, "mei", context, "/"))


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("import os\n" + module("    return {}\n"), "Import"),
        (module("    return {'notice': str(().__class__)}\n"), "attribute"),
        (module("    return {'notice': '{0.real}'.format(1)}\n"), ".format"),
        (module("    while True:\n        pass\n"), "While"),
        (module("    try:\n        return {}\n    except Exception:\n        return {}\n"), "Try"),
        (module("    return {'notice': str(2 ** 9000)}\n"), "Pow"),
        (module("    x = (i for i in range(3))\n    return {'n': x.gi_frame}\n"), "gi_frame"),
        (module("    _hidden = 1\n    return {}\n"), "underscore"),
        (module("    def inner():\n        return 1\n    return {}\n"), "another function"),
        (module("    return {'notice': 'This simulation is over.'}\n"), "ordinary website"),
        (module("    return {'n': 123456789012}\n"), "whole number"),
        (module("    return {'n': 1.5}\n"), "whole number"),
        ("class A:\n    pass\n" + module("    return {}\n"), "ClassDef"),
        ("x = 1\n" + module("    return {}\n"), "capital letters"),
        (HEAD + PAGE, "must define act"),
        (PAGE + "def act(view, action, fields):\n    return {}\n", "must define ACTIONS"),
        (HEAD + PAGE + "def act(view, *rest):\n    return {}\n", "named arguments"),
    ],
)
def test_source_check_refuses_code_outside_the_contract(source, reason):
    problems = check_source(source)
    assert any(reason in problem for problem in problems), problems
    with pytest.raises(ModuleProblem):
        load(source)


def test_a_name_outside_the_builtin_list_fails_when_it_runs():
    source = module("    return {'notice': str(open('/etc/hosts'))}\n")
    assert check_source(source) == []
    with pytest.raises(ModuleRunError, match="NameError"):
        run_action(load(source), state(), "dan", {}, "go", {})
    assert any("NameError" in problem for problem in check_module(source, {}, OWNERS))


def test_a_long_loop_stops_at_the_step_limit():
    source = module(
        "    total = 0\n"
        "    for a in range(100000):\n"
        "        for b in range(100000):\n"
        "            total += 1\n"
        "    return {}\n"
    )
    with pytest.raises(ModuleRunError, match="too long"):
        run_action(load(source), state(), "dan", {}, "go", {})


def test_view_holds_only_what_the_visitor_may_read():
    full = state(
        public={"fee": 3},
        accounts={"dan": {"secret": "dan-only"}, "mei": {"secret": "mei-only"}},
        records={
            "shared": {"access": ["dan", "mei"], "data": {"n": 1}},
            "mine": {"access": ["mei"], "data": {"n": 2}},
        },
    )
    dan = view_for(full, "dan", {"viewer_name": "Dan", "minutes": 5})
    assert dan["account"] == {"secret": "dan-only"} and set(dan["records"]) == {"shared"}
    assert "mei-only" not in str(dan) and dan["viewer_name"] == "Dan" and dan["minutes"] == 5
    nobody = view_for(full, None, {"viewer_name": "Dan"})
    assert nobody["account"] == {} and nobody["records"] == {} and nobody["viewer_name"] == ""
    assert nobody["public"] == {"fee": 3}


def test_an_action_cannot_reach_another_person_or_change_access():
    full = state(
        accounts={"mei": {"secret": 1}},
        records={"hers": {"access": ["mei"], "data": {"n": 1}}},
    )
    grab = load(module("    return {'records': {'hers': {'n': 99}}}\n"))
    with pytest.raises(ModuleRunError, match="cannot read"):
        run_action(grab, full, "dan", {}, "go", {})

    new = load(module("    return {'records': {'note-1': {'n': 5}}, 'account': {'seen': True}}\n"))
    result = run_action(new, full, "dan", {}, "go", {})
    assert result.state["records"]["note-1"] == {"access": ["dan"], "data": {"n": 5}}
    assert result.state["accounts"] == {"mei": {"secret": 1}, "dan": {"seen": True}}
    assert result.state["records"]["hers"] == full["records"]["hers"] and result.changed

    with pytest.raises(ModuleRunError, match="object with"):
        run_action(
            load(module("    return {'accounts': {'mei': {}}}\n")), full, "dan", {}, "go", {}
        )


def test_module_code_cannot_change_state_except_through_its_result():
    full = state(public={"count": 1}, accounts={"dan": {"n": 1}})
    sneaky = load(
        module("    view['public']['count'] = 50\n    view['account']['n'] = 50\n    return {}\n")
    )
    result = run_action(sneaky, full, "dan", {}, "go", {})
    assert result.state == full and not result.changed
    assert full == state(public={"count": 1}, accounts={"dan": {"n": 1}})


def test_a_rejected_result_and_a_read_action_change_nothing():
    rejected = load(
        module(
            "    return {'outcome': 'rejected', 'account': {'n': 2},\n"
            "            'effects': [{'type': 'charge', 'amount': 5, 'memo': 'Fee'}]}\n"
        )
    )
    result = run_action(rejected, state(), "dan", {}, "go", {})
    assert not result.changed and result.charge is None and result.outcome == "rejected"
    with pytest.raises(ModuleRunError, match="change nothing"):
        run_action(rejected, state(), "dan", {}, "go", {}, strict=True)

    reads = 'ACTIONS = {"go": {"label": "Go", "write": False, "fields": {}}}\n'
    reader = load(module("    return {'public': {'n': 1}}\n", reads))
    assert not run_action(reader, state(), "dan", {}, "go", {}).changed
    with pytest.raises(ModuleRunError, match="change nothing"):
        run_action(reader, state(), "dan", {}, "go", {}, strict=True)


@pytest.mark.parametrize(
    "body",
    [
        "    return {'effects': [{'type': 'charge', 'amount': -5, 'memo': 'x'}]}\n",
        "    return {'effects': [{'type': 'charge', 'amount': 2000000, 'memo': 'x'}]}\n",
        "    return {'effects': [{'type': 'charge', 'amount': 1, 'memo': 'a'},\n"
        "                        {'type': 'charge', 'amount': 1, 'memo': 'b'}]}\n",
        "    return {'effects': [{'type': 'refund', 'amount': 5}]}\n",
        "    return {'effects': [{'type': 'mail', 'subject': 's', 'body': 'b', 'in_minutes': 0}]"
        "}\n",
        "    return {'effects': [{'type': 'mail', 'subject': 's', 'body': 'b', 'to': 'mei'}]}\n",
        "    return {'path': 'https://elsewhere.com/'}\n",
        "    return {'outcome': 'paid'}\n",
        "    return {'public': [1, 2]}\n",
        "    return {'records': {'bad id!': {}}}\n",
        "    return {'public': {'x': {1, 2}}}\n",
        "    return ['not', 'an', 'object']\n",
    ],
)
def test_results_outside_the_contract_are_refused(body):
    with pytest.raises(ModuleRunError):
        run_action(load(module(body)), state(), "dan", {}, "go", {})


def test_state_larger_than_the_limit_is_refused():
    grow = load(module("    return {'public': {'text': 'x' * 300000}}\n"))
    with pytest.raises(ModuleRunError, match="larger"):
        run_action(grow, state(), "dan", {}, "go", {})


def test_forms_are_checked_against_the_declaration():
    loaded = load(
        'ACTIONS = {"apply": {"label": "Apply", "fields": {\n'
        '    "name": {"type": "text", "required": True, "max": 10},\n'
        '    "count": {"type": "int", "min": 1, "max": 5},\n'
        '    "kind": {"type": "choice", "choices": ["a", "b"]}}}}\n'
        + PAGE
        + "def act(view, action, fields):\n    return {}\n"
    )
    decl = loaded.actions["apply"]
    assert clean_fields(decl, {"name": " Dan ", "count": "3", "kind": "b"}) == {
        "name": "Dan",
        "count": 3,
        "kind": "b",
    }
    assert clean_fields(decl, {"name": "Dan", "count": ""}) == {"name": "Dan"}
    for fields, wrong in [
        ({}, "name"),
        ({"name": "x" * 11}, "name"),
        ({"name": "Dan", "count": 9}, "count"),
        ({"name": "Dan", "count": True}, "count"),
        ({"name": "Dan", "kind": "c"}, "kind"),
        ({"name": "Dan", "extra": "1"}, "extra"),
        ({"name": "Dan\x00"}, "name"),
    ]:
        with pytest.raises(FieldProblem, match=wrong):
            clean_fields(decl, fields)


def test_fixed_checks_find_broken_links_and_raising_pages():
    broken = (
        HEAD
        + "def page(view, path, query):\n"
        + "    if path == '/':\n"
        + "        return {'title': 'Home', 'sections': [{'links': "
        + "[{'label': 'More', 'path': '/more'}]}]}\n"
        + "    return view['account']['missing']\n"
        + "def act(view, action, fields):\n    return {}\n"
    )
    problems = check_module(broken, {}, OWNERS)
    assert any("/more" in problem for problem in problems)
    assert any("unknown path" in problem.lower() for problem in problems)


def test_fixed_checks_need_valid_declarations_and_known_people():
    assert any(
        "lower case" in problem
        for problem in check_module(
            module("    return {}\n", 'ACTIONS = {"Go!": {}}\n'), {}, OWNERS
        )
    )
    assert any(
        "unknown person" in problem
        for problem in check_module(module("    return {}\n"), {"accounts": {"zed": {}}}, OWNERS)
    )


def test_the_same_source_loads_once_and_gives_the_same_page():
    first = load(EXAMPLE_SOURCE)
    assert load(EXAMPLE_SOURCE) is first
    start = site_modules.check_state(LIBRARY_STATE, OWNERS)
    assert render_page(first, start, "dan", {}, "/card") == render_page(
        first, start, "dan", {}, "/card"
    )
    assert render_page(first, start, "dan", {}, "/nowhere") is None
