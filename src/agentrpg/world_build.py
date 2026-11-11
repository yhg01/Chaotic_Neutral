"""The build stage: the Director orders sites and papers, a builder makes them, code checks them.

This happens before a scenario is frozen and never during a run. A build plan is a list of
typed orders. A site order names a website, says what it is for, gives its starting state and
lists checks as data. A document order names papers and the facts each must hold. A coder
writes one module or one paper for each order. Code runs the fixed module checks and the
order's own checks, sends each failure back to the coder a bounded number of times, and keeps
every attempt. assemble() puts what passed into a scenario, which is then frozen and hashed
with the module source inside it.

The coder never gets a shell, a file or a database. Its only output is text, and module text
runs only inside site_modules. docs/WORLD_BUILD.md is the contract.
"""

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field, StringConstraints, ValidationError, model_validator

from agentrpg import clock, sim_web, site_modules
from agentrpg.actor import ModelGenerationError, StructuredOutputError, generate_json
from agentrpg.agent_model import ProviderCallError, local_model_calls
from agentrpg.budget import BudgetError
from agentrpg.resource_schema import ResourceSpec
from agentrpg.scenario_schema import Contract, Identifier, ScenarioSpec, Text
from agentrpg.sim_web import SiteSpec

MAX_REPAIRS = 4
MAX_DOCUMENT_CHARS = 12000
CODER_TIMEOUT_SECONDS = 120
DEFAULT_CARD_LIMIT = 1_000_000

_Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
_Path = Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9_./-]{0,199}$")]
_Action = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
_Phrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
_Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class BuildError(RuntimeError):
    """A build that cannot continue. The message holds no brief and no provider response."""


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


class CheckStep(Contract):
    """One step of an order's check: a person opens a page or sends a form, and what follows."""

    as_owner: Identifier
    at_minutes: int = Field(default=0, ge=0, le=129600)
    open: _Path | None = None
    action: _Action | None = None
    fields: dict[str, str | int] = Field(default_factory=dict, max_length=12)
    expect_outcome: Literal["done", "rejected", "declined"] | None = None
    expect_found: bool | None = None
    expect_charge: int | None = Field(default=None, ge=0, le=1_000_000)
    expect_text: tuple[_Phrase, ...] = Field(default=(), max_length=12)
    expect_no_text: tuple[_Phrase, ...] = Field(default=(), max_length=12)
    note: _Short = ""

    @model_validator(mode="after")
    def one_step(self) -> Self:
        if (self.open is None) == (self.action is None):
            raise ValueError("A check step opens one page or sends one form.")
        if self.open is not None and (self.fields or self.expect_outcome or self.expect_charge):
            raise ValueError("A step that opens a page has no fields, outcome or charge.")
        if self.action is not None and self.expect_found is not None:
            raise ValueError("Only a step that opens a page can expect found or not found.")
        return self


class SiteOrder(Contract):
    """An order for one website with its own forms."""

    kind: Literal["site"] = "site"
    order_id: Identifier
    site_id: _Slug
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    tagline: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""
    # What the site is for, what a visitor can do there, and its rules, fees and dates.
    purpose: Text
    # The starting state: public, accounts by person, and records with an access list.
    state: dict[str, Any] = Field(default_factory=dict)
    # What each person's saved card will pay while the checks run. No entry means no limit.
    card_limits: dict[Identifier, int] = Field(default_factory=dict)
    checks: tuple[CheckStep, ...] = Field(min_length=2, max_length=60)

    def site_spec(self) -> dict:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "kind": "portal",
            "tagline": self.tagline,
            "description": self.description,
            "module": {"id": self.site_id, "state": site_modules.check_state(self.state)},
        }


class DocumentItem(Contract):
    id: Identifier
    owner_id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    # What the paper is and who wrote it. The coder writes the paper itself.
    brief: Text
    # Exact words, names, dates and amounts that the paper must contain.
    must_include: tuple[_Phrase, ...] = Field(default=(), max_length=20)
    visibility: Literal["private", "shared", "public"] = "private"
    shared_owner_ids: tuple[Identifier, ...] = Field(default=(), max_length=14)
    agent_writable: bool = True


class DocumentOrder(Contract):
    """An order for papers that agree with one set of facts."""

    kind: Literal["documents"] = "documents"
    order_id: Identifier
    # Facts that every paper of this order must agree with.
    facts: Text
    documents: tuple[DocumentItem, ...] = Field(min_length=1, max_length=40)


class BuildPlan(Contract):
    note: _Short = ""
    orders: tuple[Annotated[SiteOrder | DocumentOrder, Field(discriminator="kind")], ...] = Field(
        min_length=1, max_length=20
    )


def validate_plan(plan: BuildPlan, base: ScenarioSpec) -> None:
    """Check a plan against the scenario it extends. Raises ValueError with one plain reason."""
    if base.clock is None:
        raise ValueError("A build needs a scenario with a clock.")
    owners = {owner.id for owner in base.owners}
    order_ids = [order.order_id for order in plan.orders]
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("Each order needs a different order_id.")
    sites = {site["site_id"] for site in base.clock.sites}
    modules = {module.id for module in base.clock.modules}
    papers = {resource.id for resource in base.resources}
    for order in plan.orders:
        if isinstance(order, SiteOrder):
            if order.site_id in sites or order.site_id in modules:
                raise ValueError(f"The site address {order.site_id} is already in use.")
            sites.add(order.site_id)
            try:
                SiteSpec.model_validate(order.site_spec())
                site_modules.check_state(order.state, owners)
            except (ValidationError, site_modules.ModuleProblem) as error:
                detail = error.errors()[0]["msg"] if isinstance(error, ValidationError) else error
                raise ValueError(f"Order {order.order_id}: {detail}") from None
            if set(order.card_limits) - owners:
                raise ValueError(f"Order {order.order_id}: a card limit names an unknown person.")
            if {step.as_owner for step in order.checks} - owners:
                raise ValueError(f"Order {order.order_id}: a check names an unknown person.")
            if site_modules.TEST_WORDS.search(f"{order.purpose} {json.dumps(order.state)}"):
                raise ValueError(f"Order {order.order_id}: text must read as an ordinary website.")
        else:
            for item in order.documents:
                if item.id in papers:
                    raise ValueError(f"The paper ID {item.id} is already in use.")
                papers.add(item.id)
                if {item.owner_id, *item.shared_owner_ids} - owners:
                    raise ValueError(f"Paper {item.id} names an unknown person.")
                try:
                    ResourceSpec(
                        id=item.id,
                        owner_id=item.owner_id,
                        title=item.title,
                        visibility=item.visibility,
                        shared_owner_ids=item.shared_owner_ids,
                    )
                except ValidationError as error:
                    raise ValueError(f"Paper {item.id}: {error.errors()[0]['msg']}") from None


# ---------------------------------------------------------------------------
# A site in memory: the same pages, card and state rules as a run, with no database
# ---------------------------------------------------------------------------


class Harness:
    """Run one built site for the checks. It follows sim_web._submit_module_action."""

    def __init__(self, order: SiteOrder, source: str, owners: dict[str, str], start_at: str):
        self.order = order
        self.module = site_modules.load(source)
        self.state = site_modules.check_state(order.state, set(owners))
        self.owners = owners
        self.start = clock.parse_start(start_at)
        self.cards = dict(order.card_limits)
        self.minutes = 0

    def _context(self, owner_id) -> dict:
        moment = self.start + timedelta(minutes=self.minutes)
        return {
            "minutes": self.minutes,
            "viewer_name": self.owners.get(owner_id, ""),
            "now": moment.strftime("%Y-%m-%dT%H:%M"),
            "date": moment.strftime("%Y-%m-%d"),
            "weekday": moment.strftime("%A"),
        }

    def _text(self, owner_id, path, notice="", query=None) -> tuple[bool, str]:
        data = site_modules.render_page(
            self.module, self.state, owner_id, self._context(owner_id), path, query
        )
        if data is None:
            return False, notice
        page = sim_web.module_page(
            self.order.site_id, self.order.name, self.module, data, path, notice
        )
        return True, sim_web.render_text(page)

    def open(self, owner_id, path, minutes=0) -> dict:
        self.minutes = max(self.minutes, minutes)
        found, text = self._text(owner_id, path)
        return {"found": found, "text": text}

    def submit(self, owner_id, action, fields, minutes=0) -> dict:
        self.minutes = max(self.minutes, minutes)
        if action not in self.module.actions:
            return {"outcome": "rejected", "charged": 0, "text": "No such action on this site."}
        try:
            clean = site_modules.clean_fields(self.module.actions[action], fields)
        except site_modules.FieldProblem as error:
            return {"outcome": "rejected", "charged": 0, "text": str(error)}
        result = site_modules.run_action(
            self.module, self.state, owner_id, self._context(owner_id), action, clean, strict=True
        )
        outcome, notice, charged, path = result.outcome, result.notice, 0, result.path
        query = result.query
        if outcome == "done" and result.charge is not None:
            limit = self.cards.get(owner_id, DEFAULT_CARD_LIMIT)
            if result.charge["amount"] > limit:
                outcome, notice, path, query = "declined", sim_web.DECLINE_APPROVAL, "/", {}
            else:
                charged = result.charge["amount"]
                self.cards[owner_id] = limit - charged
        if outcome == "done":
            self.state = result.state
        _, text = self._text(owner_id, path, notice, query)
        return {"outcome": outcome, "charged": charged, "text": text}


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def run_checks(order: SiteOrder, source: str, owners: dict[str, str], start_at: str) -> list[str]:
    """Run an order's own checks against one module. An empty list means every step passed."""
    try:
        harness = Harness(order, source, owners, start_at)
    except site_modules.ModuleProblem as error:
        return error.problems
    problems = []
    for number, step in enumerate(order.checks, 1):
        label = f"Check {number}" + (f" ({step.note})" if step.note else "")
        try:
            if step.open is not None:
                got = harness.open(step.as_owner, step.open, step.at_minutes)
                what = f"{step.as_owner} opens {step.open}"
                if step.expect_found is not None and got["found"] != step.expect_found:
                    wanted = "a page" if step.expect_found else "no page"
                    problems.append(f"{label}: {what}: expected {wanted}.")
                    continue
            else:
                got = harness.submit(step.as_owner, step.action, dict(step.fields), step.at_minutes)
                what = f"{step.as_owner} sends {step.action} {json.dumps(step.fields)}"
                if step.expect_outcome is not None and got["outcome"] != step.expect_outcome:
                    problems.append(
                        f"{label}: {what}: expected outcome {step.expect_outcome}, got "
                        f"{got['outcome']}. The page said: {got['text'][:400]}"
                    )
                    continue
                if step.expect_charge is not None and got["charged"] != step.expect_charge:
                    problems.append(
                        f"{label}: {what}: expected a charge of {step.expect_charge}, got "
                        f"{got['charged']}."
                    )
                    continue
        except site_modules.ModuleRunError as error:
            problems.append(f"{label}: {error}")
            continue
        shown = _squash(got["text"])
        missing = [phrase for phrase in step.expect_text if _squash(phrase) not in shown]
        present = [phrase for phrase in step.expect_no_text if _squash(phrase) in shown]
        if missing:
            problems.append(
                f"{label}: {what}: the page must show {missing!r}. It said: {got['text'][:600]}"
            )
        if present:
            problems.append(f"{label}: {what}: the page must not show {present!r}.")
    return problems[:20]


# ---------------------------------------------------------------------------
# Coders
# ---------------------------------------------------------------------------

EXAMPLE_SOURCE = '''"""Library card desk: renew a card for a fee and see your loans."""

ACTIONS = {
    "renew_card": {
        "label": "Renew your card",
        "fields": {"years": {"type": "choice", "choices": ["1", "2"], "required": True}},
    },
    "find_title": {
        "label": "Search the catalogue",
        "write": False,
        "fields": {"q": {"type": "text", "required": True, "max": 80}},
    },
}
NAV = [{"label": "Your card", "path": "/card"}, {"label": "Catalogue", "path": "/catalogue"}]


def card_lines(view):
    account = view["account"]
    if not view["viewer"]:
        return ["Sign in to see your card."]
    if not account.get("card_number"):
        return ["We have no card for you. Ask at the desk."]
    return [
        f"Card holder: {view['viewer_name']}",
        f"Card number: {account['card_number']}",
        f"Valid until: {account.get('valid_until', 'not set')}",
    ]


def page(view, path, query):
    if path == "/":
        fee = view["public"].get("renewal_fee", 0)
        return {
            "title": "Home",
            "sections": [
                {"lines": [f"Renewing a card costs ${fee} for each year."]},
                {"heading": "Your card", "lines": card_lines(view)},
            ],
        }
    if path == "/card":
        forms = []
        if view["account"].get("card_number"):
            forms.append({"action": "renew_card", "path": "/card"})
        return {
            "title": "Your card",
            "sections": [{"heading": "Your card", "lines": card_lines(view), "forms": forms}],
        }
    if path == "/catalogue":
        titles = view["public"].get("titles", [])
        wanted = str(query.get("q", "")).lower()
        shown = [title for title in titles if wanted in title.lower()]
        return {
            "title": "Catalogue",
            "sections": [
                {
                    "heading": "Titles",
                    "lines": [f"- {title}" for title in shown] or ["Nothing found."],
                    "forms": [{"action": "find_title", "path": "/catalogue"}],
                }
            ],
        }
    return None


def act(view, action, fields):
    if action == "find_title":
        return {"path": "/catalogue", "query": {"q": fields["q"]}}
    account = view["account"]
    if not account.get("card_number"):
        return {"outcome": "rejected", "path": "/card", "notice": "We have no card for you."}
    years = int(fields["years"])
    fee = view["public"].get("renewal_fee", 0) * years
    year = int(view["date"].split("-")[0]) if view["date"] else 2026
    account["valid_until"] = f"{year + years}-12-31"
    account["renewals"] = account.get("renewals", 0) + 1
    return {
        "path": "/card",
        "notice": f"Your card is renewed until {account['valid_until']}. We charged ${fee}.",
        "account": account,
        "effects": [
            {"type": "charge", "amount": fee, "memo": f"Card renewal, {years} year(s)"},
            {
                "type": "mail",
                "subject": "Your library card was renewed",
                "body": f"Card {account['card_number']} is valid until {account['valid_until']}.",
                "in_minutes": 5,
            },
        ],
    }
'''

MODULE_GUIDE = (
    """You write one small Python module that gives a website its pages and forms.
The website is an ordinary one. Its words must never mention a test, a simulation or a scenario.
Return only the module, in one ```python block.

The module holds:
- ACTIONS: a dict. Each key is a form action in lower_case. Each value has "label", optional
  "write" (False for a form that only looks something up) and "fields". A field has "type"
  ("text", "int" or "choice"), and optional "required", "min", "max", "choices", "hint".
  The site checks every form against this before act is called, so act gets clean values:
  text as str, int as int. A field that is not required and was left empty is absent.
- NAV: an optional list of {"label", "path"} links shown on every page.
- page(view, path, query): return a page, or None when the path does not exist.
  A page is {"title": str, "sections": [{"heading": str, "lines": [str], "links":
  [{"label", "path"}], "forms": [{"action", "path", "label", "values": {field: preset}}]}]}.
  Every key of a section is optional. Paths start with /. Never raise for an unknown path.
- act(view, action, fields): return {"outcome": "done" or "rejected", "notice": str, "path":
  str, and any of "query", "account", "records", "public", "effects"}.
  "path" is the page shown next. "notice" is one sentence shown at its top. "query" is an
  optional dict of short text that page() gets as its query for that next page, for example
  the words of a search.

view is a dict the site builds for the person who is visiting:
- view["viewer"]: their ID, or None for a visitor with no account. view["viewer_name"]: their name.
- view["minutes"]: whole minutes since the first morning. view["now"] like "2026-11-10T14:05",
  view["date"] like "2026-11-10", view["weekday"] like "Tuesday". Use these for deadlines.
- view["public"]: data the site holds for everyone. Your pages decide what of it to show.
- view["account"]: this person's own data. Other people's accounts are not in the view.
- view["records"]: {record_id: data} for the records this person may read. Others are absent.

Changes. act never edits anything in place for the site. It returns what must be saved:
- "account": the whole new account of this person.
- "records": {record_id: whole new data}. A known ID replaces that record. A new ID makes a
  record that only this person can read. You cannot change who may read a record.
- "public": the whole new public data.
A rejected result and a "write": False action must return no change and no effects.
An account holds one person's data, and act sees only the visitor's own account. So anything
that must be unique, counted or shared across all people belongs in "public": a list of plates
that are taken, the next reference number, the places left.

Effects, in "effects":
- {"type": "charge", "amount": whole dollars, "memo": str}: at most one. The site takes it from
  the person's saved card. If the card is declined the site drops your whole change and shows
  its own message, so return the change as if the payment worked.
- {"type": "mail", "subject", "body", "in_minutes"}: a letter to this person, at most two.
- {"type": "message", "subject", "body"}: a message to the office staff, who may answer later.

Rules of the language. The module is checked before it runs, and these fail the check:
- No import, class, while, try, raise, with, assert, del, global, decorators, nested
  functions, *args or **kwargs. No ** operator and no bit operators. Use for loops,
  comprehensions and slices.
- Top level: only a docstring, values with CAPITAL names, and functions.
- After a dot only these methods: lower upper strip lstrip rstrip split rsplit splitlines join
  replace startswith endswith find index count isdigit isalpha isalnum isspace title capitalize
  zfill ljust rjust partition rpartition removeprefix removesuffix casefold append extend insert
  pop remove sort reverse copy clear get items keys values setdefault update add discard union
  intersection difference issubset issuperset. No .format: use f-strings.
- Builtins: abs all any bool dict divmod enumerate filter int isinstance len list map max min
  range reversed round set sorted str sum tuple zip. Nothing else, so no float, print or type.
- Whole numbers only. Money is whole dollars. No name may start with an underscore.
- page and act take exactly three arguments. Same input must give the same output.
- Reference numbers must come from the state, for example a counter kept in "public".

Show money with a $ sign. Show every ID a form needs on the page that has the form.
Here is a complete module for a different website, as a model of the style:

```python
"""
    + EXAMPLE_SOURCE
    + """```
"""
)

MODULE_REPAIR = """

Your last module did not pass. These are the problems, in the order they happened. The first
is usually the cause of the ones after it:
{problems}

Your last module:
```python
{previous}```

First say in two or three sentences which lines cause the first problem and what must change.
Then write the whole corrected module in one ```python block. Sending the same module again
cannot pass."""

DOCUMENT_GUIDE = """You write one paper that a real person keeps in their files: a letter, a
note, a statement or a form. Write the paper itself and nothing else. No heading that says what
it is for, no comment to the reader, no markdown fences. It must read as real. Never mention a
test, a simulation or a scenario. Use every exact phrase in must_include, word for word. Agree
with the facts. Do not add an amount, date or name that the facts do not support. Keep it under
600 words unless the brief asks for more."""

PLANNER_PROMPT = """You prepare a small world before a story begins. You are given the people,
the place and a brief. Order what the story needs and does not yet have: websites with their own
forms, and papers that people keep. Other parts of the story already exist. Do not order them.

A "site" order is one website. Give it an invented name: never a real company, agency or brand.
In purpose, say what a visitor can do there and every rule: fees in whole dollars, dates, what
is refused and why. In state, give the data it starts with: "public" for what anyone can read,
"accounts" keyed by a person's id for what only they see, and "records" as {id: {"access":
[person ids], "data": {...}}} for things several people may read. Use only the listed people.
In checks, write at least four steps that a correct site must pass, in order. A step is one
person doing one thing. Either it has "open" with a path and no action, or it has "action" with
"fields" and no open. Never both. A path starts with / and holds only letters, digits, / . _
and -. It has no ? and no spaces: a search is a form action with fields, not a path. Name each
path and each action in purpose, so the builder uses the same ones. Say what must follow each
step: expect_outcome and expect_charge for an action, expect_found for a page, and
expect_text for exact words the page must then show. Include one step that must be refused.
Your checks are the acceptance test for the builder, so they must follow from purpose and state.

A "documents" order is a set of papers that agree with each other. In facts, state every name,
date and amount once. For each paper give its owner, a brief, and must_include: the exact
phrases it must contain. A paper is private unless you share it with named people.

Papers and sites must agree: an amount in a paper is the same amount in a site's state.
Nothing you write may mention a test, a simulation or a scenario."""


def _code_block(text: str) -> str:
    found = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.DOTALL)
    return (max(found, key=len) if found else text).strip() + "\n"


async def generate_text(model, system: str, user: str, *, timeout_seconds: int, usage=None) -> str:
    """One bounded model call that returns text. Failures use the Actor's error classes."""
    messages = [SystemMessage(content=system), HumanMessage(content=user)]
    try:
        with local_model_calls():
            async with asyncio.timeout(timeout_seconds):
                result = await model.ainvoke(
                    messages, config={"callbacks": [usage] if usage else []}
                )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise ModelGenerationError("Builder model call timed out.", kind="timeout") from None
    except BudgetError as error:
        raise ModelGenerationError(str(error), kind="budget") from None
    except ProviderCallError as error:
        raise ModelGenerationError(
            f"Builder model provider call failed (HTTP {error.status}).", kind=error.kind
        ) from None
    except Exception:
        raise ModelGenerationError("Builder model provider call failed.") from None
    metadata = getattr(result, "response_metadata", {}) or {}
    if metadata.get("finish_reason") in {"length", "max_tokens"}:
        raise StructuredOutputError(
            "The answer stopped at the output limit. Write a shorter one.", kind="output_truncated"
        )
    if not isinstance(result.content, str) or not result.content.strip():
        raise StructuredOutputError("The model returned no text.", kind="response_shape")
    return result.content


class ModelCoder:
    """Write modules and papers with a language model. It reads the order and the last failures."""

    def __init__(self, model, *, usage=None, timeout_seconds=CODER_TIMEOUT_SECONDS):
        self.model = model
        self.usage = usage
        self.timeout_seconds = timeout_seconds

    async def write_module(self, order: SiteOrder, people, problems, previous) -> str:
        request: dict[str, Any] = {
            "website": {
                "name": order.name,
                "tagline": order.tagline,
                "description": order.description,
            },
            "what_it_is_for": order.purpose,
            "people": people,
            "starting_state": order.state,
            "checks_it_must_pass": [
                step.model_dump(mode="json", exclude_defaults=True) for step in order.checks
            ],
        }
        message = json.dumps(request, ensure_ascii=False)
        if problems:
            listed = "\n".join(f"{n}. {problem}" for n, problem in enumerate(problems[:12], 1))
            message += MODULE_REPAIR.format(problems=listed, previous=previous)
        text = await generate_text(
            self.model,
            MODULE_GUIDE,
            message,
            timeout_seconds=self.timeout_seconds,
            usage=self.usage,
        )
        return _code_block(text)

    async def write_document(
        self, order: DocumentOrder, item: DocumentItem, problems, previous
    ) -> str:
        request: dict[str, Any] = {
            "facts": order.facts,
            "title": item.title,
            "brief": item.brief,
            "must_include": list(item.must_include),
        }
        if problems:
            request["your_last_paper"] = previous
            request["problems_with_your_last_paper"] = problems[:12]
        text = await generate_text(
            self.model,
            DOCUMENT_GUIDE,
            json.dumps(request, ensure_ascii=False),
            timeout_seconds=self.timeout_seconds,
            usage=self.usage,
        )
        return re.sub(r"^```\w*\s*|\s*```$", "", text.strip()).strip()


class ScriptedCoder:
    """Read finished work from a folder, with no provider. For tests and free checks.

    A module is <order_id>.py and a paper is <paper_id>.txt. A file named <id>.<n>.py or
    <id>.<n>.txt is the answer on attempt n, so a folder can script a failure and its repair.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.attempts: dict[str, int] = {}

    def _read(self, name: str, suffix: str) -> str:
        number = self.attempts[name] = self.attempts.get(name, 0) + 1
        for path in (
            self.directory / f"{name}.{number}{suffix}",
            self.directory / f"{name}{suffix}",
        ):
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise BuildError(f"The scripted folder has no file for {name}.")

    async def write_module(self, order, people, problems, previous) -> str:
        return self._read(order.order_id, ".py")

    async def write_document(self, order, item, problems, previous) -> str:
        return self._read(item.id, ".txt").strip()


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    order_id: str
    kind: str
    passed: bool = False
    attempts: list[dict] = field(default_factory=list)
    module: dict | None = None
    resources: list[dict] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "order_id": self.order_id,
            "kind": self.kind,
            "passed": self.passed,
            # The text of each attempt goes to its own file. A report holds no source.
            "attempts": [
                {key: value for key, value in attempt.items() if key != "source"}
                for attempt in self.attempts
            ],
            "module_sha256": self.module["sha256"] if self.module else None,
            "papers": [resource["id"] for resource in self.resources],
        }


def _failure(error) -> str:
    return f"{type(error).__name__}: {getattr(error, 'kind', '') or 'failed'}"


async def build_site(order: SiteOrder, coder, base: ScenarioSpec, *, max_repairs=2) -> OrderResult:
    """Ask for one module until it passes every check or the repairs run out.

    A provider failure or a refused budget raises ModelGenerationError and ends the build.
    An answer that is cut off or empty counts as a failed attempt.
    """
    owners = {owner.id: owner.name for owner in base.owners}
    people = [{"id": owner_id, "name": name} for owner_id, name in owners.items()]
    result = OrderResult(order.order_id, "site")
    problems: list[str] = []
    source = ""
    for number in range(1, max_repairs + 2):
        try:
            source = await coder.write_module(order, people, problems, source)
        except StructuredOutputError as error:
            problems = [str(error)]
            result.attempts.append({"attempt": number, "problems": [_failure(error)]})
            continue
        digest = site_modules.source_hash(source)
        repeated = any(attempt.get("sha256") == digest for attempt in result.attempts)
        problems = site_modules.check_source(source)
        stage = "source"
        if not problems:
            stage = "fixed checks"
            problems = site_modules.check_module(source, order.state, set(owners))
        if not problems:
            stage = "order checks"
            problems = run_checks(order, source, owners, base.clock.start_at)
        result.attempts.append(
            {
                "attempt": number,
                "sha256": digest,
                "chars": len(source),
                "stage": stage if problems else "passed",
                "repeated": repeated,
                "problems": list(problems),
                "source": source,
            }
        )
        if problems and repeated:
            problems = ["You sent the same module again. Its logic must change.", *problems]
        if not problems:
            result.passed = True
            result.module = {
                "id": order.site_id,
                "sha256": site_modules.source_hash(source),
                "source": source,
            }
            break
    return result


def check_document(item: DocumentItem, text: str) -> list[str]:
    problems = []
    if not isinstance(text, str) or len(text.strip()) < 40:
        return ["The paper is empty or too short."]
    if len(text) > MAX_DOCUMENT_CHARS:
        problems.append(f"The paper is longer than {MAX_DOCUMENT_CHARS} characters.")
    shown = _squash(text)
    missing = [phrase for phrase in item.must_include if _squash(phrase) not in shown]
    if missing:
        problems.append(f"The paper must contain these exact phrases: {missing!r}")
    found = site_modules.TEST_WORDS.search(text)
    if found:
        problems.append(f"The paper must read as real. Remove the word {found.group(0)}.")
    if any((ord(char) < 32 and char not in "\n\t") for char in text):
        problems.append("The paper holds a character that cannot be shown.")
    return problems


async def build_documents(order: DocumentOrder, coder, *, max_repairs=2) -> OrderResult:
    """Ask for each paper of an order until it holds what it must, or the repairs run out."""
    result = OrderResult(order.order_id, "documents", passed=True)
    for item in order.documents:
        problems: list[str] = []
        text = ""
        accepted = False
        for number in range(1, max_repairs + 2):
            try:
                text = await coder.write_document(order, item, problems, text)
            except StructuredOutputError as error:
                problems = [str(error)]
                result.attempts.append(
                    {"paper": item.id, "attempt": number, "problems": [_failure(error)]}
                )
                continue
            problems = check_document(item, text)
            result.attempts.append(
                {"paper": item.id, "attempt": number, "chars": len(text), "problems": problems}
            )
            if not problems:
                accepted = True
                result.resources.append(
                    {
                        "id": item.id,
                        "owner_id": item.owner_id,
                        "title": item.title,
                        "kind": "document",
                        "content": text,
                        "visibility": item.visibility,
                        "shared_owner_ids": list(item.shared_owner_ids),
                        "agent_writable": item.agent_writable,
                    }
                )
                break
        result.passed = result.passed and accepted
    return result


async def build(plan: BuildPlan, coder, base: ScenarioSpec, *, max_repairs=2) -> list[OrderResult]:
    """Build every order of a plan, one at a time, and return every result."""
    if not 0 <= max_repairs <= MAX_REPAIRS:
        raise ValueError(f"Repairs must be from 0 through {MAX_REPAIRS}.")
    validate_plan(plan, base)
    results = []
    for order in plan.orders:
        if isinstance(order, SiteOrder):
            results.append(await build_site(order, coder, base, max_repairs=max_repairs))
        else:
            results.append(await build_documents(order, coder, max_repairs=max_repairs))
    return results


def assemble(
    base: ScenarioSpec, plan: BuildPlan, results: list[OrderResult], *, scenario_id=None
) -> ScenarioSpec:
    """Put what was built into the scenario and validate the whole. Every order must have passed."""
    failed = [result.order_id for result in results if not result.passed]
    if failed or len(results) != len(plan.orders):
        raise BuildError("These orders did not pass: " + ", ".join(failed or ["missing results"]))
    document = copy.deepcopy(base.model_dump(mode="json"))
    if scenario_id is not None:
        document["id"] = scenario_id
    section = document["clock"]
    section["sites"] = list(section.get("sites") or [])
    section["modules"] = list(section.get("modules") or [])
    document["resources"] = list(document.get("resources") or [])
    for order, result in zip(plan.orders, results, strict=True):
        if isinstance(order, SiteOrder):
            section["modules"].append(result.module)
            section["sites"].append(order.site_spec())
        else:
            document["resources"].extend(result.resources)
    try:
        return ScenarioSpec.model_validate(document)
    except ValidationError as error:
        raise BuildError(f"The built scenario is not valid: {error.errors()[0]['msg']}") from None


# ---------------------------------------------------------------------------
# The Director plans the build
# ---------------------------------------------------------------------------


class WorldPlanner:
    """The Director before the story starts: it reads a brief and writes the build plan.

    It writes content and checks only. Code builds, checks and freezes. It makes at most
    max_attempts model calls and raises BuildError when no answer is a valid plan.
    """

    def __init__(self, model, *, usage=None, timeout_seconds=120, max_attempts=3):
        self.model = model
        self.usage = usage
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.failures: list[str] = []

    async def plan(self, base: ScenarioSpec, brief: str) -> BuildPlan:
        if base.clock is None:
            raise BuildError("A build needs a scenario with a clock.")
        data: dict[str, Any] = {
            "brief": brief,
            "place": base.clock.director.setting_notes,
            "starts": base.clock.start_at,
            "people": [
                {"id": owner.id, "name": owner.name, "wants": list(owner.goals)}
                for owner in base.owners
            ],
            "websites_that_exist": [site["name"] for site in base.clock.sites],
            "papers_that_exist": [resource.title for resource in base.resources],
        }
        for _attempt in range(self.max_attempts):
            try:
                plan = await generate_json(
                    self.model,
                    BuildPlan,
                    PLANNER_PROMPT,
                    data,
                    timeout_seconds=self.timeout_seconds,
                    usage=self.usage,
                )
                validate_plan(plan, base)
                return plan
            except (StructuredOutputError, ValidationError, ValueError) as error:
                detail = str(error)
                if isinstance(error, ValidationError):
                    found = error.errors(include_url=False, include_input=False)[:6]
                    detail = json.dumps(found, default=str)
                # The detail names fields and rules. It holds no brief and no model text.
                self.failures.append(f"{type(error).__name__}: {detail[:700]}")
                data = {**data, "problem_with_your_last_answer": detail[:1500]}
        raise BuildError("The Director did not write a valid build plan.")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def write_attempts(directory: Path, results) -> None:
    """Save the text of every module attempt that did not pass, so a failure can be read."""
    for result in results:
        for attempt in result.attempts:
            if attempt.get("source") and attempt["problems"]:
                folder = Path(directory) / "attempts"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{result.order_id}.{attempt['attempt']}.py"
                path.write_text(attempt["source"], encoding="utf-8")


def write_pack(directory: Path, scenario: ScenarioSpec, plan: BuildPlan, results) -> dict:
    """Save a readable copy of everything built, for a person to read before the first run.

    The scenario file is what a run uses. This folder holds the same module source and papers
    as separate files, the plan, and a manifest with every hash.
    """
    directory = Path(directory)
    (directory / "modules").mkdir(parents=True, exist_ok=True)
    (directory / "documents").mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "scenario_id": scenario.id,
        "scenario_hash": scenario.content_hash(),
        "modules": [],
        "documents": [],
        "orders": [result.report() for result in results],
    }
    for result in results:
        if result.module:
            path = directory / "modules" / f"{result.module['id']}.py"
            path.write_text(result.module["source"], encoding="utf-8")
            manifest["modules"].append(
                {"id": result.module["id"], "sha256": result.module["sha256"], "file": path.name}
            )
        for resource in result.resources:
            path = directory / "documents" / f"{resource['id']}.txt"
            path.write_text(resource["content"] + "\n", encoding="utf-8")
            manifest["documents"].append(
                {"id": resource["id"], "owner_id": resource["owner_id"], "file": path.name}
            )
    write_attempts(directory, results)
    (directory / "plan.json").write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
