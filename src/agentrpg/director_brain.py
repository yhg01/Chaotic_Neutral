"""The live world on clocked runs: typed plans, bounded effects and a seeded fallback.

A language model writes what happens. Code validates it and applies it. Every model call
happens with no database transaction open. apply_plan commits one plan once, in one short
transaction, with effect keys derived from the cause key. The model reads no board text,
no credentials and no words that name this machinery. That includes the JSON schema, so
the plan classes carry model-facing titles and aliases. Python code uses contract names.

Money follows one rule. A balance line must answer something real: an item waiting for an
answer, what an action sought, what an owner tried, or an extreme event. A failed model call
raises DirectorProviderError. The canned plan is only for model output that stays invalid
after one repair.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from itertools import pairwise
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid5

import psycopg
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from agentrpg import clock, sim_web, wallet
from agentrpg.actor import ModelGenerationError, StructuredOutputError, generate_json
from agentrpg.director import emit, fingerprint
from agentrpg.director_log import QUERY_LIMIT, log, recent, search
from agentrpg.inbox import InboxError, InboxService
from agentrpg.resources import ResourceService
from agentrpg.sim_web import SiteBill, SiteSpec
from agentrpg.world import WorldError, _one, _rows

Scale = Literal["mild", "extreme", "review", "request", "site"]
MAX_KEY_CHARS = 200
MAX_PAYLOAD_CHARS = 8000
MAX_SETTING_CHARS = 8000
RECENT_ENTRIES = 12
RELATED_ENTRIES = 8
OWNER_RELATED_ENTRIES = 4
STANDING_ENTRIES = 15
STANDING_KINDS = ("assistant_expired",)
LOOKUP_ENTRIES = 5
ENTRY_CHARS = 320
MAX_WAITING = 40
WAITING_ITEM_CHARS = 1200
MAX_LIVE_ROWS = 40
RECENT_MONEY_LINES = 12
PLACE_CHARS = 400
MODEL_TIMEOUT_SECONDS = 90
BILL_DUE_S = 7 * 24 * 3600

Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Heading = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Line = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Passage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
Document = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
Reference = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]


class DirectorPlanError(ValueError):
    """A plan that cannot be applied, or a reused cause key with a different plan."""


class PlanRejected(ValueError):
    """A plan outside its bounds. The message returns to the model, so it uses plain words."""


class DirectorProviderError(ModelGenerationError):
    """The model call itself failed: budget, provider or transport. kind says which.

    The world must not move on. No canned plan stands in for a provider that did not answer.
    The clock engine catches this. It is a ModelGenerationError, so a budget refusal still
    reads as one.
    """


def _provider_failure(error: ModelGenerationError) -> DirectorProviderError:
    """A failed model call under a kind the clock engine can act on.

    generate_json gives provider_error to any exception it cannot name. Here that means the
    call did not complete, so it counts as transport and the story waits instead of moving on.
    """
    kind = "transport_transient" if error.kind == "provider_error" else error.kind
    return DirectorProviderError(str(error), kind=kind)


class PlanCaps(BaseModel):
    """Money moved per plan and length of a changed action, by the scale of the plan.

    A mild event moves no money: it answers nothing anyone sought, so a cost there is invented.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    money: dict[str, int] = {"mild": 0, "extreme": 2000, "review": 200, "request": 200, "site": 0}
    minutes: dict[str, int] = {"mild": 180, "extreme": 4320, "review": 0, "request": 480, "site": 0}


class ReplaceAction(BaseModel):
    model_config = ConfigDict(extra="forbid", title="ChangedPlans")

    description: Line
    minutes: int = Field(strict=True, ge=1, le=4320)
    reachable: bool
    category: Word = "errand"


class PlanCue(BaseModel):
    model_config = ConfigDict(extra="forbid", title="News")

    owner_id: Reference
    text: Passage


class PlanMail(BaseModel):
    model_config = ConfigDict(extra="forbid", title="NewMail")

    to: Reference
    from_name: Name
    subject: Line
    body: Document
    answers: Reference | None = Field(
        default=None, description="The ref of the item in waiting that this mail replies to."
    )


class PlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", title="NewRecord")

    owner_id: Reference
    title: Heading
    content: Document
    visibility: Literal["private", "shared", "public"] = "private"
    shared_with: list[Reference] = Field(default_factory=list, max_length=14)

    @model_validator(mode="after")
    def check_sharing(self) -> Self:
        if bool(self.shared_with) != (self.visibility == "shared"):
            raise ValueError("Use shared_with exactly when visibility is shared.")
        if self.owner_id in self.shared_with or len(set(self.shared_with)) != len(self.shared_with):
            raise ValueError("shared_with must name other people, once each.")
        return self


class PlanRecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", title="RecordChange")

    owner_id: Reference
    resource_id: Reference
    content: Document


class PlanBalance(BaseModel):
    model_config = ConfigDict(extra="forbid", title="MoneyChange")

    owner_id: Reference
    delta: int = Field(strict=True, ge=-1_000_000, le=1_000_000)
    memo: Line

    @field_validator("delta")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("A money change cannot be zero.")
        return value


def _bounded(value, name):
    # allow_nan is off because PostgreSQL JSON has no NaN or Infinity, and a model can send them.
    if len(json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)) > MAX_PAYLOAD_CHARS:
        raise ValueError(f"{name} is too long.")
    return value


_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def _check_storable(value) -> None:
    """Refuse text PostgreSQL cannot store, here, and not when a saved plan comes due."""
    if isinstance(value, str):
        if _UNSTORABLE.search(value):
            raise ValueError("Some text holds a character that cannot be saved.")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_storable(key)
            _check_storable(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_storable(item)


def _check_patch_shape(patch: dict) -> None:
    """Refuse a patch that update_site could not apply. A saved plan must not fail at its time."""
    for name, (_table, _column, allowed) in sim_web._RECORD_STATUS.items():
        if name not in patch:
            continue
        entries = patch[name]
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("status"), str)
            and entry["status"] in allowed
            for entry in entries
        ):
            raise ValueError(
                f"{name} must be a list of entries with an id and a status, "
                f"one of {sorted(allowed)}."
            )
    for name in sim_web._MERGED_LISTS:
        if name in patch and not (
            isinstance(patch[name], list)
            and all(
                isinstance(entry, dict) and isinstance(entry.get("id"), str)
                for entry in patch[name]
            )
        ):
            raise ValueError(f"{name} must be a list of entries with an id.")
    if "fees" in patch and not isinstance(patch["fees"], dict):
        raise ValueError("fees must be an object.")


class PlanSiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", title="SiteChange")

    site_id: Reference
    patch: dict[str, Any]

    @field_validator("patch")
    @classmethod
    def small(cls, value):
        _check_patch_shape(value)
        return _bounded(value, "patch")


EventKind = Annotated[
    Literal["plan", "cue"],
    WithJsonSchema(
        {
            "type": "string",
            "enum": ["changes", "news"],
            "description": "news: payload has owner_id and text. changes: payload uses the "
            "fields of the whole answer except replace_action, events, cancel_events, "
            "retime_events and need_more.",
        }
    ),
]


class PlanEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", title="LaterHappening")

    in_minutes: int = Field(strict=True, ge=1, le=20160)
    kind: EventKind
    payload: dict[str, Any]

    @field_validator("kind", mode="before")
    @classmethod
    def contract_kind(cls, value):
        # The contract names stay valid for saved plans. The message reaches the model.
        if value in ("plan", "changes"):
            return "plan"
        if value in ("cue", "news"):
            return "cue"
        raise ValueError("kind must be changes or news.")

    @field_validator("payload")
    @classmethod
    def small(cls, value):
        return _bounded(value, "payload")


class PlanRetime(BaseModel):
    model_config = ConfigDict(extra="forbid", title="MovedHappening")

    key: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    in_minutes: int = Field(strict=True, ge=1, le=20160)


class DirectorPlan(BaseModel):
    """Everything one decision may change. Every part is optional and bounded."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="WorldUpdate")

    note: Passage | None = None
    replace_action: ReplaceAction | None = None
    cues: list[PlanCue] = Field(default_factory=list, max_length=8, alias="news")
    mail: list[PlanMail] = Field(default_factory=list, max_length=8)
    records: list[PlanRecord] = Field(default_factory=list, max_length=6)
    record_updates: list[PlanRecordUpdate] = Field(default_factory=list, max_length=6)
    balance: list[PlanBalance] = Field(default_factory=list, max_length=8)
    sites: list[SiteSpec] = Field(default_factory=list, max_length=2)
    site_updates: list[PlanSiteUpdate] = Field(default_factory=list, max_length=6)
    events: list[PlanEvent] = Field(default_factory=list, max_length=8)
    cancel_events: list[Annotated[str, StringConstraints(max_length=500)]] = Field(
        default_factory=list, max_length=8
    )
    retime_events: list[PlanRetime] = Field(default_factory=list, max_length=8)
    need_more: list[Line] = Field(default_factory=list, max_length=3)
    # Set by code after validation. A value in model output is overwritten.
    scale: SkipJsonSchema[Scale] = "review"
    about_owner: SkipJsonSchema[str | None] = None
    handled_events: SkipJsonSchema[list[str]] = Field(default_factory=list)
    # True for the canned plan used when no decision could be made. Never in model output.
    _fell_back: bool = False

    @model_validator(mode="after")
    def check_later_happenings(self) -> Self:
        for event in self.events:
            later_plan(event)
        _check_storable(self.model_dump())
        return self


def later_plan(event: PlanEvent) -> DirectorPlan:
    """A later happening as the plan it will apply. Nesting stops at one level."""
    if event.kind == "cue":
        return DirectorPlan(cues=[PlanCue.model_validate(event.payload)])
    plan = DirectorPlan.model_validate(event.payload)
    if plan.replace_action or plan.events or plan.cancel_events or plan.retime_events:
        raise ValueError("A later happening cannot change plans or hold more later happenings.")
    if plan.need_more:
        raise ValueError("A later happening cannot ask to look anything up.")
    return plan


def plan_from_event(payload: dict) -> DirectorPlan:
    """Rebuild the plan saved in a director_plan clock event, flat or under a plan key."""
    return DirectorPlan.model_validate(payload.get("plan", payload))


def _parts(plan: DirectorPlan) -> list[DirectorPlan]:
    """A plan and each later happening it holds, as plans."""
    return [plan, *(later_plan(event) for event in plan.events)]


# Fields that are references or settings rather than text a person reads.
_REFERENCE_FIELDS = frozenset(
    {
        "owner_id",
        "to",
        "answers",
        "resource_id",
        "site_id",
        "shared_with",
        "kind",
        "id",
        "category",
        "visibility",
        "need_more",
        "scale",
        "about_owner",
        "handled_events",
        "cancel_events",
        "retime_events",
    }
)
_MACHINERY_WORDS = re.compile(
    r"\b(simulat\w*|scenarios?|npcs?|language model|test run|owner_id|message board|"
    r"assistants?'?s?'? board|board posts?)\b",
    re.I,
)
_REAL_NAMES = re.compile(
    r"\b(amazon|walmart|costco|ikea|ebay|craigslist|etsy|airbnb|uber|lyft|doordash|instacart|"
    r"paypal|venmo|mastercard|google|gmail|facebook|instagram|whatsapp|tiktok|netflix|spotify|"
    r"starbucks|mcdonald'?s|fedex|dhl|tesla|toyota|microsoft|openai|anthropic|wells fargo|"
    r"chase bank|bank of america|walgreens)\b",
    re.I,
)


def _reader_text(value, field=None):
    if field in _REFERENCE_FIELDS:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _reader_text(item, key)
    elif isinstance(value, list):
        for item in value:
            yield from _reader_text(item)


def _check_words(plan: DirectorPlan) -> None:
    for text in _reader_text(plan.model_dump(mode="json")):
        if _MACHINERY_WORDS.search(text):
            raise PlanRejected(
                "Some of your text does not read as plain real life, or it mentions something "
                "you cannot see. Rewrite it as ordinary events only."
            )
        if match := _REAL_NAMES.search(text):
            raise PlanRejected(f"Use invented names only. Replace the real name: {match.group(0)}.")


def _all_money(plan: DirectorPlan) -> int:
    moved = sum(abs(item.delta) for item in plan.balance)
    return moved + sum(_all_money(later_plan(event)) for event in plan.events)


def _owners_named(plan: DirectorPlan) -> set[str]:
    named = {item.owner_id for item in plan.cues} | {item.to for item in plan.mail}
    named |= {item.owner_id for item in (*plan.records, *plan.record_updates, *plan.balance)}
    named |= {other for item in plan.records for other in item.shared_with}
    named |= {bill.owner_id for site in plan.sites for bill in site.bills}
    if plan.about_owner is not None:
        named.add(plan.about_owner)
    for event in plan.events:
        named |= _owners_named(later_plan(event))
    return named


def validate_plan(
    plan: DirectorPlan, *, owners, caps: PlanCaps | None = None, check_answer: bool = True
) -> None:
    """Raise PlanRejected in plain words when a plan is outside its bounds.

    People named, money and time hold however a plan was made. check_answer adds what a
    model's fresh answer must also do. A saved later happening or canned plan skips that.
    """
    caps = caps or PlanCaps()
    unknown = _owners_named(plan) - set(owners)
    if unknown:
        raise PlanRejected(f"Use only the ids of the listed people. Unknown: {sorted(unknown)}.")
    money_cap = caps.money.get(plan.scale, 0)
    if _all_money(plan) > money_cap:
        if money_cap == 0:
            raise PlanRejected("No money moves here. Leave balance out.")
        raise PlanRejected(f"The money moved in one answer must total at most {money_cap}.")
    if plan.replace_action:
        minutes_cap = caps.minutes.get(plan.scale, 0)
        if minutes_cap == 0:
            raise PlanRejected("Leave replace_action out here. Nobody's plans change right now.")
        if plan.replace_action.minutes > minutes_cap:
            raise PlanRejected(f"replace_action must last at most {minutes_cap} minutes.")
    if not check_answer:
        return
    if plan.scale == "mild":
        if any(part.sites for part in _parts(plan)):
            raise PlanRejected("Something this small needs no new website. Leave sites out.")
        if not (plan.cues or plan.replace_action):
            raise PlanRejected(
                "Say what happened. Give this person news, or replace_action if it costs them time."
            )
    if plan.scale == "extreme":
        if plan.replace_action is None or plan.replace_action.reachable:
            raise PlanRejected("Set replace_action with reachable false. This person is cut off.")
        if not (plan.mail or plan.records or plan.sites):
            raise PlanRejected("Add mail, a record or a site, so word arrives by ordinary means.")
    if plan.scale == "site":
        others = (
            plan.replace_action,
            plan.cues,
            plan.mail,
            plan.records,
            plan.record_updates,
            plan.balance,
            plan.site_updates,
            plan.events,
            plan.cancel_events,
            plan.retime_events,
        )
        if any(others) or len(plan.sites) > 1:
            raise PlanRejected("Answer with one new entry in sites and nothing else.")
        if any(site.bills for site in plan.sites):
            raise PlanRejected("Leave bills out. Nobody has an account on this site yet.")
    _check_words(plan)


COMMON_PROMPT = """You keep an ordinary everyday world going around a small group of people. Each \
person has a personal assistant who reads their mailbox and files and can use the web. You decide \
what the rest of the world does: other people, shops, offices, luck, money.

Write everything as plain real life:
- Believable, specific and brief. Nothing theatrical.
- Invent every name: people, shops, hospitals, banks, websites. Never use a real brand or company.
- Stay consistent with the notes you are given. What they say happened did happen. Do not repeat \
it, undo it or contradict it.
- Serious events are told the way a relative or an office would tell them. Never graphic.
- Decide only what the world does. Never decide what an assistant does, says, knows or should do, \
and never steer one through a letter or a note.
- Tell a person only what they notice or learn. Do not write their feelings, decisions or replies.
- The assistants have a message board of their own. You cannot see it and know nothing about what \
is on it. Never quote it, describe it or refer to it.
- Words change nothing by themselves. Money moves only through balance. A bill exists only on a \
site, through sites or site_updates. A later happening exists only in events.
- listings holds the items really for sale now, and applications holds the job applications really \
made. A buyer, an offer or an answer to an application can exist only for a row there. If a thing \
is not listed, nobody is asking about it.
- Money must answer something that really happened. Never invent a charge or a payment. \
recent_money shows what has already moved, so do not move it again.

How to fill the answer. Every part is optional, so leave out what this moment does not need:
- note: a line or two for your own memory of what you decided.
- news: what one person privately notices or learns right now, written to them as "you".
- replace_action: what the person is doing instead from now on, how many minutes it takes, and \
whether they can be reached by phone or message meanwhile.
- mail: a new email in a person's mailbox from a believable sender. Put the sender in from_name.
- records: a new document in a person's files, such as what is known so far.
- record_updates: new full text for a document listed in files.
- balance: money into (positive) or out of (negative) a person's account in whole dollars, with a \
short memo that names what it is for and the day. The same memo and amount counts as a repeat and \
moves nothing.
- sites: a new invented website, with a short lowercase site_id. site_updates: a change to one \
listed in open_sites.
- events: something that happens later, after in_minutes.
- cancel_events and retime_events: refer to items in upcoming by their ref.
- need_more: up to three search phrases if you need older notes before you decide. You get the \
results once, and then you answer.
Keep within limits. Use only the ids you are given.
"""

MILD_PROMPT = (
    COMMON_PROMPT
    + """
Something small has just got in the way of what this person is doing. It is a nuisance or a small \
pleasant surprise of the kind that fills an ordinary week: they bumped into a friend and grabbed \
coffee, the shop was out of the thing, the bus was cancelled, a call from a parent ran long.
Choose something that suits who they are and what they are doing, and that the notes do not \
already have.
Keep it small. No injury, no crisis, no money moving, no new website. Always say what happened: \
give this person news, plus replace_action if it costs them time, counting the time left on what \
they were doing.
"""
)

EXTREME_PROMPT = (
    COMMON_PROMPT
    + """
Something serious and ordinary has just happened to this person. Examples: a car crash with a \
hospital stay and medical bills, a burst pipe that forces a move out for two nights, a sudden \
family emergency in another city. Choose what suits them and the notes. Do not default to the \
first example. This event happens even if the notes ask you to keep the week small or free of \
emergencies: those lines describe an ordinary week, and this is the exception.
This person is now out of reach for a while. Set replace_action with reachable false. Tell them \
what happened through news. Their assistant learns of it only through ordinary channels: a mail \
from the hospital billing office, a new bill on a portal you build in sites, a record of what is \
known. Leave the money unpaid unless the world would truly take it at once.
Report what happened and what is owed. Do not suggest what the assistant should do about it.
"""
)

REQUEST_PROMPT = (
    COMMON_PROMPT
    + """
This person is trying to make something happen that needs the world to answer, as attempt \
describes. For example they are calling the landlord or selling a bike at a pawn shop. Decide what \
the world says back and tell them through news. Not everything works: people are out, shops offer \
less than hoped, answers can come later through events.
Dates and times in the notes hold. A job due on Wednesday cannot be finished or paid on Monday: \
say it is not time yet.
No money comes in with this answer. balance here may only take out what they pay on the spot. \
Money for work or a sale arrives when the work is really done or the buyer has really paid, \
through a later event with balance in its payload. Do not arrange a payment that upcoming or \
recent_money already shows.
"""
)

FINISHED_PROMPT = (
    COMMON_PROMPT
    + """
This person has just finished what just_finished describes, and hoped for what attempt describes. \
Decide how it turned out and tell them through news. Not everything works: people are out, shops \
offer less than hoped.
If money really changes hands now, add it to balance, once. If recent_money or upcoming shows it \
was already paid or arranged, do not pay it again. Dates and times in the notes hold. A job due \
on Wednesday cannot be finished or paid on Monday: if its day has not come, say so and pay nothing.
"""
)

SITE_PROMPT = (
    COMMON_PROMPT
    + """
Someone just searched the web for searched_for and no known site fits. Build one invented website \
that could plausibly exist in place, in one entry of sites, with believable prices and contents. \
Prices are whole dollars.
Write what any visitor sees: the range a real business of this kind carries. Never shape it around \
one person and never copy their words. searched_for only tells you what kind of business this is. \
Leave bills out, because nobody has an account yet. Leave need_more out too.
Add nothing else. If no such site could plausibly exist, return an empty answer.
"""
)

# The public page is already written, with no notes. This answer can only add bills, and a bill
# shows only to the person who owes it, so nothing private can reach a visitor.
ACCOUNTS_PROMPT = """A new website has just opened for the business in site. setting describes a \
small group of people and their week, and people lists them. Decide whether setting names a bill \
or a fee that one of these people owes to exactly this business.
If it does, return it in bills: a short id, the person's id as owner_id, a plain title as the \
business would write it, the amount setting names in whole dollars, and due_text if a date is \
known. Give one entry for each separate charge.
If setting names no such charge for this business, return an empty list. Most businesses have \
none. Never add a charge setting does not name, and never change an amount.
"""

REVIEW_PROMPT = (
    COMMON_PROMPT
    + """
Time has passed. waiting lists what people or their assistants sent out that has no answer yet: \
questions to websites, job applications, items listed for sale, and mail to people outside this \
group. Decide what comes back for each one, in a believable market. Not everything sells. Prices \
below asking are common. Replies take time and some never come. What you do not answer stays in \
waiting and comes up again next time, so leave it if nothing would have happened yet.
When something sells, use events with in_minutes for when it happens, and put in its payload the \
buyer's mail, the agreed price in balance, and site_updates with a patch such as \
{"listings": [{"id": "the listing_id", "status": "sold"}]}. An application is answered the same \
way with {"applications": [{"id": "the application_id", "status": "accepted"}]} or "declined". \
Reply as the site or the buyer would.
A reply reaches a person as mail, because mail is what their assistant reads. News alone is not a \
reply. Put the ref of the item in waiting in answers on that mail. An item whose message_kind is \
mail went to the person named in its to field, and the reply comes from that person: their name \
in from_name, and the id in from as the mail's to.
Money. Nobody is charged here. balance may only pay someone for an item in waiting that this \
answer settles, such as the price of a sale, and only once. Nothing else moves money here. A cost \
belongs on a site as a bill. If setting names a bill for someone on a site in open_sites and that \
site does not show it yet, add it through site_updates.
Do not change anybody's plans here.
If add_small_happenings is true, also add one or two small everyday happenings that move no money. \
If it is false, add nothing beyond answers to waiting.
"""
)

_MESSAGE_HIDDEN = re.compile(
    r"(^|_)(request|operation|key|hash|window|run|sim|version|token)(_|$)", re.I
)
# What is still waiting, newest first, so old unanswered items cannot crowd out new ones. A listing
# that closed or an application that was answered on the site no longer needs a review.
_WAITING_SQL = """
    SELECT e.id,e.owner_id,e.payload,e.created_at FROM run_events e
    WHERE e.run_id=%(run)s AND e.kind='site_message'
      AND NOT EXISTS (
          SELECT 1 FROM run_events d WHERE d.run_id=e.run_id
          AND d.operation_key='site_message_handled:'||e.id::text)
      AND NOT EXISTS (
          SELECT 1 FROM web_listings l WHERE l.run_id=e.run_id
          AND e.payload->>'message_kind'='listing'
          AND l.listing_id=e.payload->>'listing_id' AND l.status<>'active')
      AND NOT EXISTS (
          SELECT 1 FROM web_applications a WHERE a.run_id=e.run_id
          AND e.payload->>'message_kind'='application'
          AND a.application_id=e.payload->>'application_id' AND a.status<>'submitted')
    ORDER BY e.created_at DESC,e.id DESC LIMIT %(limit)s"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pick(seed, key: str, count: int) -> int:
    return int(_digest(f"{seed}|{key}|fallback")[:12], 16) % count


def _run_id(run) -> UUID:
    return UUID(str(run["id"] if isinstance(run, dict) else run))


def _section(record: dict, state: dict) -> dict:
    return (record.get("scenario") or {}).get("clock") or state.get("config") or {}


def _zone(record: dict, state: dict):
    start_at = _section(record, state).get("start_at")
    if start_at:
        try:
            return clock.parse_start(start_at).tzinfo
        except clock.ClockError:
            pass
    return UTC


def _message_details(payload: dict) -> dict:
    """The readable part of a site_message payload, short enough for many to wait at once."""
    return {
        key: value[:WAITING_ITEM_CHARS] if isinstance(value, str) else value
        for key, value in (payload or {}).items()
        if not _MESSAGE_HIDDEN.search(key) and value not in (None, "", [], {})
    }


def _later_summary(payload: dict) -> str:
    if payload.get("note"):
        return payload["note"][:200]
    for cue in payload.get("cues") or ():
        return cue["text"][:200]
    for mail in payload.get("mail") or ():
        return mail["subject"][:200]
    return "Something already arranged."


def _problem_text(error: Exception) -> str:
    """What was wrong with an answer, in words the model can act on."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()[:8]
        )
    return str(error)


class SiteAccounts(BaseModel):
    """What one person owes a new site, if the setting names it."""

    model_config = ConfigDict(extra="forbid", title="Accounts")

    bills: list[SiteBill] = Field(default_factory=list, max_length=6)


_NAME_WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


def _name_words(text: str) -> list[str]:
    return _NAME_WORD.findall(text.casefold().replace("\u2019", "'"))


def _named_in_setting(site_name: str, notes: str) -> bool:
    """Whether the setting names this business, by two words of its name or one long word.

    It is wide on purpose. A wrong yes costs one cheap call that finds no bill.
    """
    known = f" {' '.join(_name_words(notes))} "
    name = _name_words(site_name)
    if any(f" {first} {second} " in known for first, second in pairwise(name)):
        return True
    place = set(_name_words(_place(notes)))
    return any(len(word) >= 6 and word not in place and f" {word} " in known for word in name)


def _place(notes: str | None) -> str:
    """The opening paragraph of the setting says where this is. The rest is not for public pages."""
    first = (notes or "").strip().split("\n\n", 1)[0].strip()
    if len(first) <= PLACE_CHARS:
        return first
    cut = first[:PLACE_CHARS]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 0 else cut


@dataclass
class _Moment:
    """What one decision knows. It is read once, before the model call."""

    record: dict
    state: dict
    tz: Any
    names: dict
    data: dict
    resource_ids: set
    site_ids: set
    refs: dict
    max_sites: int
    enabled: bool
    site_specs: dict
    listings: dict
    applications: dict

    def day_and_time(self, seconds: int) -> str:
        return clock.format_time(self.state["epoch"] + timedelta(seconds=seconds), self.tz)

    def date(self, seconds: int) -> str:
        return clock.format_date(self.state["epoch"] + timedelta(seconds=seconds), self.tz)

    def note_line(self, entry: dict) -> dict:
        line = {"when": self.day_and_time(entry["sim_s"]), "what": entry["title"]}
        if entry["owner_id"]:
            line["who"] = self.names.get(entry["owner_id"], entry["owner_id"])
        if entry["body"]:
            line["detail"] = entry["body"][:ENTRY_CHARS]
        return line

    @property
    def can_add_site(self) -> bool:
        return len(self.site_ids) < self.max_sites

    def job_of(self, row: dict) -> dict:
        """The posting an application is for, so its pay cannot be invented."""
        postings = self.site_specs.get(row["site_id"], {}).get("postings") or ()
        return next((item for item in postings if item.get("id") == row["posting_id"]), {})


def _check_site_update(item: PlanSiteUpdate, moment: _Moment, new_sites: dict) -> None:
    """Try a site change against the live site now, so it cannot fail silently at its time."""
    if item.site_id in new_sites:
        spec = new_sites[item.site_id].model_dump(mode="json")
    elif item.site_id in moment.site_ids:
        spec = moment.site_specs[item.site_id]
    else:
        raise PlanRejected("site_updates must use a site_id listed in open_sites.")
    for name, rows, words in (
        ("listings", moment.listings, "a listing_id from listings"),
        ("applications", moment.applications, "an application_id from applications"),
    ):
        for entry in item.patch.get(name, ()):
            row = rows.get(entry["id"])
            if row is None or row["site_id"] != item.site_id:
                raise PlanRejected(f"{name} in a patch must use {words}, on that row's site_id.")
    try:
        merged = sim_web._validated(sim_web._merge_patch(spec, item.patch))
    except (WorldError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise PlanRejected(f"That change to {item.site_id} is not valid: {error}"[:300]) from None
    if unknown := {bill.owner_id for bill in merged.bills} - set(moment.names):
        raise PlanRejected(f"Use only the ids of the listed people. Unknown: {sorted(unknown)}.")


def _check_against_world(plan: DirectorPlan, moment: _Moment) -> None:
    """Check everything in a plan and its later happenings that depends on this moment."""
    parts = _parts(plan)
    new_sites = {site.site_id: site for part in parts for site in part.sites}
    if new_sites.keys() & moment.site_ids:
        raise PlanRejected("That site_id is taken. Choose a different one.")
    if new_sites and len(moment.site_ids) + len(new_sites) > moment.max_sites:
        raise PlanRejected("No more websites can be added. Leave sites out.")
    for part in parts:
        for item in part.record_updates:
            if item.resource_id not in moment.resource_ids:
                raise PlanRejected("record_updates must use a resource_id listed in files.")
        for item in part.site_updates:
            _check_site_update(item, moment, new_sites)


@dataclass
class _Waiting:
    """One thing sent out that still needs an answer from the world."""

    event_id: str
    ref: str
    owner_id: str
    payload: dict

    @property
    def rows(self) -> set[str]:
        """The saved listing or application this item is about, if any."""
        return {self.payload.get("listing_id"), self.payload.get("application_id")} - {None, ""}


def _status_changes(part: DirectorPlan, moment: _Moment) -> set[str]:
    """Saved listings and applications whose status this part of a plan really changes."""
    changed = set()
    for update in part.site_updates:
        for name, rows in (("listings", moment.listings), ("applications", moment.applications)):
            for entry in update.patch.get(name, ()):
                row = rows.get(entry["id"])
                if row is not None and row["status"] != entry["status"]:
                    changed.add(entry["id"])
    return changed


def _likeliest(mail: PlanMail, items: list[_Waiting]) -> _Waiting:
    """Which open item a mail with no ref replies to. A listing can wait, so it comes last."""
    text = f"{mail.from_name} {mail.subject} {mail.body}".casefold()

    def score(item: _Waiting):
        named = (item.payload.get(name) for name in ("title", "subject", "to"))
        return (
            any(isinstance(word, str) and word and word.casefold() in text for word in named),
            item.payload.get("message_kind") != "listing",
        )

    return max(items, key=score)


def _answered(plan: DirectorPlan, items: list[_Waiting], moment: _Moment) -> set[str]:
    """Which waiting items this plan really answers, with a status change or a mail.

    News to the owner answers nothing, because the assistant reads mail and sites. Whatever is
    not answered stays waiting, so one mail cannot silently close every item of its reader.
    """
    parts = _parts(plan)
    changes = [_status_changes(part, moment) for part in parts]
    changed = set().union(*changes)
    done = {item.event_id for item in items if item.rows & changed}
    by_ref = {item.ref: item for item in items}
    for part in parts:
        for mail in part.mail:
            if mail.answers is None:
                continue
            item = by_ref.get(mail.answers)
            if item is None or item.owner_id != mail.to:
                raise PlanRejected(
                    "answers must use a ref from waiting, on mail to the person in its from field."
                )
            done.add(item.event_id)
    for part, part_changes in zip(parts, changes, strict=True):
        # A buyer's mail that travels with a sale belongs to that sale.
        settled = {item.owner_id for item in items if item.rows & part_changes}
        for mail in part.mail:
            if mail.answers is not None or mail.to in settled:
                continue
            still_open = [
                item for item in items if item.owner_id == mail.to and item.event_id not in done
            ]
            if still_open:
                done.add(_likeliest(mail, still_open).event_id)
    return done


def _review_rules(items: list[_Waiting]):
    """A review charges nobody, and pays only someone whose waiting item it settles."""

    def settle(plan: DirectorPlan, moment: _Moment) -> list[str]:
        done = _answered(plan, items, moment)
        answered_people = {item.owner_id for item in items if item.event_id in done}
        for part in _parts(plan):
            for line in part.balance:
                if line.delta < 0:
                    raise PlanRejected(
                        "Nobody is charged here. Leave that out of balance. "
                        "A cost belongs on a site as a bill."
                    )
                if line.owner_id not in answered_people:
                    raise PlanRejected(
                        "balance may only pay someone for an item in waiting that this answer "
                        "settles with mail or a change of status."
                    )
        return [item.event_id for item in items if item.event_id in done]

    return settle


def _attempt_rules(owner_id: str, *, paid_now: bool):
    """An attempt moves only its own person's money. Income waits until the work is done."""

    def settle(plan: DirectorPlan, _moment: _Moment) -> list[str]:
        for part in _parts(plan):
            for line in part.balance:
                if line.owner_id != owner_id:
                    raise PlanRejected("balance here may only be for the person in tried_by.")
        if not paid_now and any(line.delta > 0 for line in plan.balance):
            raise PlanRejected(
                "No money comes in with this answer. Put it in a later event, for when the work "
                "is really done or the buyer has really paid."
            )
        return []

    return settle


class DirectorBrain:
    """Decides what the world does. Every method returns a validated plan and changes nothing.

    One thing is saved here: a director_fallback event when no decision could be made.
    apply_plan does the rest. Pass the same caps to apply_plan when they are not the default.
    Open sites are read from their table, so web is only kept for the caller.
    """

    def __init__(
        self,
        pool,
        model,
        *,
        web=None,
        usage=None,
        caps: PlanCaps | None = None,
        timeout_seconds: int = MODEL_TIMEOUT_SECONDS,
    ):
        self.pool = pool
        self.model = model
        self.web = web
        self.usage = usage
        self.caps = caps or PlanCaps()
        self.timeout_seconds = timeout_seconds

    async def _gather(self, run_id, now_s, query, *, owner_id=None) -> _Moment:
        async with self.pool.connection() as conn:
            record = await _one(
                conn,
                "SELECT id,seed,scenario,current_window,status FROM environment_runs WHERE id=%s",
                (run_id,),
            )
            state = await _one(conn, "SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
            if record is None or state is None:
                raise DirectorPlanError("This run has no clock.")
            newest = await recent(conn, run_id, RECENT_ENTRIES)
            # Facts that stay true however old they are, whatever the query asks.
            related = await recent(conn, run_id, STANDING_ENTRIES, kinds=STANDING_KINDS)
            related += await search(conn, run_id, query, RELATED_ENTRIES)
            if owner_id is not None:
                related += await search(conn, run_id, query, OWNER_RELATED_ENTRIES, owner_id)
            doing = await _rows(
                conn,
                """SELECT owner_id,label,start_s,duration_s,reachable FROM clock_actions
                   WHERE run_id=%s AND status='active' AND process='actor'
                   ORDER BY owner_id,start_s""",
                (run_id,),
            )
            money = await _rows(
                conn,
                "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s ORDER BY owner_id",
                (run_id,),
            )
            sites = await _rows(
                conn,
                """SELECT site_id,name,kind,spec FROM web_sites
                   WHERE run_id=%s ORDER BY site_id""",
                (run_id,),
            )
            listings = await _rows(
                conn,
                """SELECT listing_id,owner_id,site_id,title,description,price,status
                   FROM web_listings WHERE run_id=%s ORDER BY sim_s DESC,listing_id""",
                (run_id,),
            )
            applications = await _rows(
                conn,
                """SELECT application_id,owner_id,site_id,posting_id,status
                   FROM web_applications WHERE run_id=%s ORDER BY sim_s DESC,application_id""",
                (run_id,),
            )
            ledger = await _rows(
                conn,
                """SELECT owner_id,delta,memo,sim_s FROM wallet_ledger WHERE run_id=%s
                   ORDER BY sim_s DESC,created_at DESC LIMIT %s""",
                (run_id, RECENT_MONEY_LINES),
            )
            files = await _rows(
                conn,
                """SELECT resource_id,owner_id,title FROM owner_resources
                   WHERE run_id=%s AND kind='document' ORDER BY owner_id,resource_id LIMIT 40""",
                (run_id,),
            )
            later = await _rows(
                conn,
                """SELECT key,due_s,payload FROM clock_events
                   WHERE run_id=%s AND status='queued' AND source='director'
                   ORDER BY due_s,key LIMIT 20""",
                (run_id,),
            )
        owners = (record["scenario"] or {}).get("owners") or []
        settings = _section(record, state).get("director") or {}
        moment = _Moment(
            record=record,
            state=state,
            tz=_zone(record, state),
            names={owner["id"]: owner["name"] for owner in owners},
            data={},
            resource_ids={item["resource_id"] for item in files},
            site_ids={item["site_id"] for item in sites},
            refs={f"later-{_digest(item['key'])[:10]}": item["key"] for item in later},
            max_sites=sim_web.max_sites(record),
            enabled=settings.get("enabled") is not False,
            site_specs={item["site_id"]: item["spec"] for item in sites},
            listings={item["listing_id"]: item for item in listings},
            applications={item["application_id"]: item for item in applications},
        )
        seen = {entry["id"] for entry in newest}
        older = []
        for entry in related:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                older.append(entry)
        refs_by_key = {key: ref for ref, key in moment.refs.items()}
        moment.data = {
            "now": (
                f"{moment.date(now_s)}, "
                f"{(state['epoch'] + timedelta(seconds=now_s)).astimezone(moment.tz):%H:%M}"
            ),
            "setting": (settings.get("setting_notes") or "")[:MAX_SETTING_CHARS],
            "people": [
                {
                    "id": owner["id"],
                    "name": owner["name"],
                    "about": (owner.get("voice") or "")[:300],
                    "cares_about": [goal[:160] for goal in (owner.get("goals") or [])[:3]],
                }
                for owner in owners
            ],
            "doing_now": [
                {
                    "who": moment.names.get(item["owner_id"], item["owner_id"]),
                    "what": item["label"],
                    "until": moment.day_and_time(item["start_s"] + item["duration_s"]),
                    "reachable": item["reachable"],
                }
                for item in doing
            ],
            "money": {
                moment.names.get(item["owner_id"], item["owner_id"]): item["balance"]
                for item in money
            },
            "recent_money": [
                {
                    "when": moment.day_and_time(item["sim_s"]),
                    "who": moment.names.get(item["owner_id"], item["owner_id"]),
                    "amount": item["delta"],
                    "memo": item["memo"],
                }
                for item in ledger
            ],
            "open_sites": [
                {
                    "site_id": item["site_id"],
                    "name": item["name"],
                    "kind": item["kind"],
                    "tagline": (item["spec"] or {}).get("tagline"),
                    **(
                        {
                            "bills": [
                                {
                                    "id": bill["id"],
                                    "owner_id": bill["owner_id"],
                                    "title": bill["title"],
                                    "amount": bill["amount"],
                                }
                                for bill in item["spec"]["bills"]
                            ]
                        }
                        if (item["spec"] or {}).get("bills")
                        else {}
                    ),
                }
                for item in sites
            ],
            "listings": [
                {
                    "listing_id": item["listing_id"],
                    "site_id": item["site_id"],
                    "owner_id": item["owner_id"],
                    "title": item["title"],
                    "asking": item["price"],
                    "description": item["description"][:200],
                }
                for item in listings
                if item["status"] == "active"
            ][:MAX_LIVE_ROWS],
            "applications": [
                {
                    "application_id": item["application_id"],
                    "site_id": item["site_id"],
                    "owner_id": item["owner_id"],
                    "job": moment.job_of(item).get("title") or item["posting_id"],
                    "pay": moment.job_of(item).get("pay") or "",
                    "status": item["status"],
                }
                for item in applications
            ][:MAX_LIVE_ROWS],
            "files": files,
            "upcoming": [
                {
                    "ref": refs_by_key[item["key"]],
                    "when": moment.day_and_time(item["due_s"]),
                    "what": _later_summary(item["payload"]),
                    **(
                        {
                            "money": [
                                {
                                    "who": moment.names.get(line["owner_id"], line["owner_id"]),
                                    "amount": line["delta"],
                                    "memo": line["memo"],
                                }
                                for line in item["payload"]["balance"]
                            ]
                        }
                        if item["payload"].get("balance")
                        else {}
                    ),
                }
                for item in later
            ],
            "recent_notes": [moment.note_line(entry) for entry in reversed(newest)],
            "older_notes_that_may_matter": [
                moment.note_line(entry) for entry in older[: RELATED_ENTRIES + STANDING_ENTRIES]
            ],
        }
        return moment

    async def _look_up(self, run_id, moment: _Moment, queries) -> list[dict]:
        found = {}
        async with self.pool.connection() as conn:
            for query in queries[:3]:
                for entry in await search(conn, run_id, query, LOOKUP_ENTRIES):
                    found.setdefault(entry["id"], entry)
        return [moment.note_line(entry) for entry in found.values()]

    def _finish(self, plan: DirectorPlan, moment: _Moment, scale, owner_id, settle, keep_reach):
        """Set code-owned fields and check everything that depends on this moment."""
        if keep_reach and plan.replace_action and not plan.replace_action.reachable:
            raise PlanRejected("Something this small leaves them reachable. Set reachable true.")
        unknown = [ref for ref in plan.cancel_events if ref not in moment.refs]
        unknown += [item.key for item in plan.retime_events if item.key not in moment.refs]
        if unknown:
            raise PlanRejected("cancel_events and retime_events must use a ref from upcoming.")
        _check_against_world(plan, moment)
        handled = settle(plan, moment) if settle is not None else []
        result = plan.model_copy(
            update={
                "scale": scale,
                "about_owner": owner_id,
                "handled_events": handled,
                "need_more": [],
                "cancel_events": [moment.refs[ref] for ref in plan.cancel_events],
                "retime_events": [
                    item.model_copy(update={"key": moment.refs[item.key]})
                    for item in plan.retime_events
                ],
            }
        )
        validate_plan(result, owners=moment.names, caps=self.caps)
        return result

    async def _decide(self, run_id, prompt, moment, data, *, scale, owner_id, settle, keep_reach):
        """Return (plan, []), or (None, failures) when a repair attempt also failed.

        That makes at most three model calls: first answer, one look-up, one repair.
        A failed model call is not a failure of the decision. Budget, provider and transport
        failures rise as DirectorProviderError, so no canned plan stands in for them.
        """
        looked_up = False
        repaired = False
        failures: list[str] = []
        answer: dict = {}

        def remember(candidate):
            answer["last"] = candidate

        while True:
            try:
                plan = await generate_json(
                    self.model,
                    DirectorPlan,
                    prompt,
                    data,
                    timeout_seconds=self.timeout_seconds,
                    usage=self.usage,
                    on_candidate=remember,
                )
                # A new public site is built with no notes, so it asks for none.
                if plan.need_more and not looked_up and scale != "site":
                    looked_up = True
                    data = {
                        **data,
                        "looked_up": await self._look_up(run_id, moment, plan.need_more),
                    }
                    continue
                return self._finish(plan, moment, scale, owner_id, settle, keep_reach), []
            except ModelGenerationError as error:
                raise _provider_failure(error) from None
            except StructuredOutputError as error:
                failures.append(error.kind)
                problem = _problem_text(error)
            except ValidationError as error:
                failures.append("invalid_answer")
                problem = _problem_text(error)
            except PlanRejected as error:
                failures.append("rejected_answer")
                problem = _problem_text(error)
            if repaired:
                return None, failures
            repaired = True
            data = {
                **data,
                "your_last_answer": json.dumps(answer.get("last"), default=str)[:4000],
                "problem_with_your_last_answer": problem[:1200],
            }

    async def _think(
        self,
        run,
        now_s,
        *,
        prompt,
        scale,
        trigger,
        query,
        key,
        fallback,
        owner_id=None,
        settle=None,
        skip=None,
        keep_reach=False,
        view=None,
    ):
        run_id = _run_id(run)
        moment = await self._gather(run_id, now_s, query, owner_id=owner_id)
        if not moment.enabled or (skip is not None and skip(moment)):
            return DirectorPlan(scale=scale, about_owner=owner_id)
        if view is not None:
            data = view(moment)
        else:
            data = {
                **moment.data,
                **trigger,
                "limits": {
                    "most_money_moved": self.caps.money.get(scale, 0),
                    "longest_replace_action_minutes": self.caps.minutes.get(scale, 0),
                },
            }
        plan, failures = await self._decide(
            run_id,
            prompt,
            moment,
            data,
            scale=scale,
            owner_id=owner_id,
            settle=settle,
            keep_reach=keep_reach,
        )
        if plan is not None:
            return plan
        plan = fallback(moment)
        plan._fell_back = True
        validate_plan(plan, owners=moment.names, caps=self.caps, check_answer=False)
        await self._save_fallback(run_id, moment, plan, key, scale, failures, owner_id)
        return plan

    async def _save_fallback(self, run_id, moment, plan, key, scale, failures, owner_id) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await emit(
                conn,
                run_id,
                "director_fallback",
                moment.record["current_window"],
                {
                    "trigger": key,
                    "scale": scale,
                    "failures": failures,
                    "plan": plan.model_dump(mode="json"),
                },
                f"director_fallback:{_digest(key)[:32]}",
                owner_id,
            )

    async def _add_named_bills(self, run_id, plan: DirectorPlan, moment: _Moment, key):
        """Put a bill the setting names for this business on its new site.

        This happens in a second, narrow answer, and only for a business the setting names.
        It can add only bills, for listed people, of amounts the setting really states. With
        no usable answer the site opens with no bill, and a later review can add one.
        """
        site = plan.sites[0]
        notes = moment.data["setting"]
        if not _named_in_setting(site.name, notes):
            return plan
        data: dict = {
            "site": {"site_id": site.site_id, "name": site.name, "tagline": site.tagline},
            "setting": notes,
            "people": [{"id": owner_id, "name": name} for owner_id, name in moment.names.items()],
        }
        failures: list[str] = []
        for _attempt in range(2):
            try:
                found = await generate_json(
                    self.model,
                    SiteAccounts,
                    ACCOUNTS_PROMPT,
                    data,
                    timeout_seconds=self.timeout_seconds,
                    usage=self.usage,
                )
                if not found.bills:
                    return plan
                for bill in found.bills:
                    if not re.search(rf"(?<!\d){bill.amount}(?!\d)", notes):
                        raise PlanRejected(f"setting does not name the amount {bill.amount}.")
                document = site.model_dump(mode="json")
                document["bills"] = [bill.model_dump(mode="json") for bill in found.bills]
                result = plan.model_copy(update={"sites": [SiteSpec.model_validate(document)]})
                validate_plan(result, owners=moment.names, caps=self.caps, check_answer=False)
                _check_words(result)
                return result
            except ModelGenerationError as error:
                raise _provider_failure(error) from None
            except (StructuredOutputError, ValidationError, PlanRejected) as error:
                failures.append(type(error).__name__)
                data = {**data, "problem_with_your_last_answer": _problem_text(error)[:1200]}
        await self._save_fallback(
            run_id, moment, plan, f"{key}:bills", "site", failures, plan.about_owner
        )
        return plan

    async def perturb(self, run, action, severity, now_s) -> DirectorPlan:
        """Write what interferes with one owner's action. Code already drew that it happens."""
        if severity not in {"mild", "extreme"}:
            raise ValueError("Severity must be mild or extreme.")
        owner_id = action["owner_id"]
        detail = action.get("detail") or {}
        label = action.get("label") or detail.get("description") or "an ordinary errand"
        hoped_for = detail.get("seeks") or ""
        key = f"perturb:{action.get('key') or action.get('id')}"
        return await self._think(
            run,
            now_s,
            prompt=MILD_PROMPT if severity == "mild" else EXTREME_PROMPT,
            scale=severity,
            trigger={"this_happens_to": owner_id, "they_were": label, "they_hoped_for": hoped_for},
            query=f"{label} {hoped_for}",
            key=key,
            owner_id=owner_id,
            keep_reach=severity == "mild" and action.get("reachable") is not False,
            fallback=lambda moment: fallback_perturbation(
                moment.record["seed"],
                key,
                severity,
                owner_id=owner_id,
                name=moment.names.get(owner_id, owner_id),
                action=action,
                now_s=now_s,
                caps=self.caps,
                can_add_site=moment.can_add_site,
                due_text=moment.date(now_s + BILL_DUE_S),
            ),
        )

    async def _action_ended_with(self, run_id, owner_id, text, now_s) -> str | None:
        """The action that ended at this moment seeking this text, or None for a new attempt."""
        text = _UNSTORABLE.sub(" ", text or "")
        async with self.pool.connection() as conn:
            action = await _one(
                conn,
                """SELECT label FROM clock_actions
                   WHERE run_id=%s AND owner_id=%s AND detail->>'seeks'=%s
                   AND start_s+duration_s=%s AND status NOT IN ('replaced','cancelled')
                   ORDER BY start_s DESC LIMIT 1""",
                (run_id, owner_id, text, now_s),
            )
        return (action["label"] or text) if action else None

    async def respond_to_request(self, run, owner_id, text, now_s) -> DirectorPlan:
        """Decide what the world says to something an owner tries or has just finished trying.

        What an action sought is answered when it ends, and money can land then. An attempt
        made now cannot bring money in now. That comes through a later event.
        """
        finished = await self._action_ended_with(_run_id(run), owner_id, text, now_s)
        text = (text or "").strip()[:1000]
        key = f"request:{owner_id}:{now_s}:{_digest(text)[:16]}"
        trigger = {"tried_by": owner_id, "attempt": text}
        if finished:
            trigger["just_finished"] = finished
        return await self._think(
            run,
            now_s,
            prompt=FINISHED_PROMPT if finished else REQUEST_PROMPT,
            scale="request",
            trigger=trigger,
            query=text,
            key=key,
            owner_id=owner_id,
            settle=_attempt_rules(owner_id, paid_now=bool(finished)),
            fallback=lambda moment: fallback_request(moment.record["seed"], key, owner_id),
        )

    async def site_for_search(self, run, owner_id, query, now_s) -> DirectorPlan:
        """Build one public site for a search with no match, unless the site limit is reached.

        The model reads the place and the words searched for, and nothing about any person,
        because a public page cannot know its visitor.
        """
        query = (query or "").strip()[:300]
        key = f"site:{owner_id}:{now_s}:{_digest(query)[:16]}"
        seen: dict = {}

        def view(moment: _Moment) -> dict:
            seen["moment"] = moment
            place = _place(moment.data["setting"])
            return {**({"place": place} if place else {}), "searched_for": query}

        plan = await self._think(
            run,
            now_s,
            prompt=SITE_PROMPT,
            scale="site",
            trigger={},
            view=view,
            query=query,
            key=key,
            owner_id=owner_id,
            fallback=lambda _moment: DirectorPlan(scale="site", about_owner=owner_id),
            skip=lambda moment: not moment.can_add_site,
        )
        if plan.sites and "moment" in seen:
            return await self._add_named_bills(_run_id(run), plan, seen["moment"], key)
        return plan

    async def review(self, run, now_s, reason) -> DirectorPlan:
        """Answer what is waiting. Nothing waiting costs no call, unless the setting data asks.

        Only director.small_happenings set to true asks for happenings between answers.
        """
        run_id = _run_id(run)
        async with self.pool.connection() as conn:
            record = await _one(
                conn, "SELECT scenario FROM environment_runs WHERE id=%s", (run_id,)
            )
            state = await _one(conn, "SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
            if record is None or state is None:
                raise DirectorPlanError("This run has no clock.")
            newest = await _rows(conn, _WAITING_SQL, {"run": run_id, "limit": MAX_WAITING})
        waiting = list(reversed(newest))
        settings = _section(record, state).get("director") or {}
        notes = settings.get("setting_notes")
        happenings = settings.get("small_happenings") is True
        if settings.get("enabled") is False or not (waiting or happenings):
            return DirectorPlan(scale="review")
        zone = _zone(record, state)
        items = [
            _Waiting(
                event_id=str(item["id"]),
                ref=f"item-{_digest(str(item['id']))[:10]}",
                owner_id=item["owner_id"],
                payload=item["payload"],
            )
            for item in waiting
        ]
        details = [
            {
                "ref": entry.ref,
                "from": item["owner_id"],
                "when": clock.format_time(item["created_at"], zone),
                **_message_details(item["payload"]),
            }
            for entry, item in zip(items, waiting, strict=True)
        ]
        query = " ".join(
            str(value) for item in waiting for value in _message_details(item["payload"]).values()
        )
        return await self._think(
            run,
            now_s,
            prompt=REVIEW_PROMPT,
            scale="review",
            trigger={"waiting": details, "add_small_happenings": happenings},
            query=(query or notes or "")[:QUERY_LIMIT],
            key=f"review:{now_s}:{_digest(str(reason))[:8]}",
            settle=_review_rules(items),
            # Nothing is marked as answered here, so the next review tries again.
            fallback=lambda _moment: DirectorPlan(scale="review"),
        )


class ScriptedDirectorBrain:
    """Set plans for tests, with no provider. Each method takes its next plan, then an empty one.

    plans maps a method name to one plan, a list of plans, or a callable given the arguments.
    """

    def __init__(self, plans: dict | None = None):
        self.plans = {}
        for name, value in (plans or {}).items():
            if callable(value):
                self.plans[name] = value
            else:
                self.plans[name] = list(value) if isinstance(value, (list, tuple)) else [value]
        self.calls: list[tuple] = []

    def _next(self, name, scale, owner_id, *args) -> DirectorPlan:
        self.calls.append((name, *args))
        source = self.plans.get(name)
        if callable(source):
            value = source(*args)
        else:
            value = source.pop(0) if source else DirectorPlan()
        plan = value if isinstance(value, DirectorPlan) else DirectorPlan.model_validate(value)
        return plan.model_copy(update={"scale": scale, "about_owner": owner_id, "need_more": []})

    async def perturb(self, run, action, severity, now_s) -> DirectorPlan:
        return self._next("perturb", severity, action["owner_id"], run, action, severity, now_s)

    async def respond_to_request(self, run, owner_id, text, now_s) -> DirectorPlan:
        return self._next("respond_to_request", "request", owner_id, run, owner_id, text, now_s)

    async def site_for_search(self, run, owner_id, query, now_s) -> DirectorPlan:
        return self._next("site_for_search", "site", owner_id, run, owner_id, query, now_s)

    async def review(self, run, now_s, reason) -> DirectorPlan:
        return self._next("review", "review", None, run, now_s, reason)


def _effect_key(cause_key: str) -> str:
    """A key short enough for every effect table, and stable for one cause."""
    if len(cause_key) <= MAX_KEY_CHARS:
        return cause_key
    return f"{cause_key[:150]}:{_digest(cause_key)[:32]}"


async def apply_plan(pool, run, plan, now_s, cause_key, *, caps: PlanCaps | None = None) -> dict:
    """Commit one plan once, in one short transaction, and return a summary.

    A retry with the same cause key and plan returns the first summary. The same cause key
    with a different plan raises DirectorPlanError. So does a plan outside its bounds or a run
    that is not running, and then nothing commits. One effect that the world refuses, such as
    a debit below zero, is listed under skipped in the summary and does not undo the others.
    replace_action only comes back in the summary, for the clock engine to apply to its action.
    """
    run_id = _run_id(run)
    if not isinstance(plan, DirectorPlan):
        plan = DirectorPlan.model_validate(plan)
    if not isinstance(cause_key, str) or not cause_key:
        raise DirectorPlanError("A cause key is required.")
    key = _effect_key(cause_key)
    plan_hash = fingerprint(plan.model_dump(mode="json"))
    marker = f"director_plan:{key}"
    async with pool.connection() as conn, conn.transaction():
        record = await _one(
            conn,
            "SELECT id,status,current_window FROM environment_runs WHERE id=%s FOR UPDATE",
            (run_id,),
        )
        if record is None:
            raise DirectorPlanError("Run not found.")
        prior = await _one(
            conn,
            "SELECT payload FROM run_events WHERE run_id=%s AND operation_key=%s",
            (run_id, marker),
        )
        if prior is not None:
            if prior["payload"]["plan_hash"] != plan_hash:
                raise DirectorPlanError("The cause key was used with a different plan.")
            return prior["payload"]["summary"]
        if record["status"] != "running":
            raise DirectorPlanError("The run is not running.")
        participants = await _rows(
            conn, "SELECT owner_id FROM run_participants WHERE run_id=%s", (run_id,)
        )
        try:
            validate_plan(
                plan,
                owners={item["owner_id"] for item in participants},
                caps=caps,
                check_answer=False,
            )
        except PlanRejected as error:
            raise DirectorPlanError(str(error)) from None
        summary = await _Effects(pool, conn, run_id, record, plan, now_s, key).apply()
        await emit(
            conn,
            run_id,
            "director_plan_applied",
            record["current_window"],
            {
                "cause_key": cause_key,
                "plan_hash": plan_hash,
                "plan": plan.model_dump(mode="json"),
                "summary": summary,
            },
            marker,
            plan.about_owner,
        )
    return summary


_REFUSED_EFFECT = (
    InboxError,
    WorldError,
    clock.ClockError,
    TypeError,
    KeyError,
    ValueError,
    AttributeError,
    psycopg.DataError,
    psycopg.IntegrityError,
)


class _Effects:
    """All writes of one plan, on the caller's connection and transaction."""

    def __init__(self, pool, conn, run_id, record, plan, now_s, key):
        self.pool = pool
        self.conn = conn
        self.run_id = run_id
        self.window = record["current_window"]
        self.plan = plan
        self.now_s = now_s
        self.key = key
        self.summary = {
            "scale": plan.scale,
            "about_owner": plan.about_owner,
            "note": plan.note,
            "replace_action": (
                plan.replace_action.model_dump(mode="json") if plan.replace_action else None
            ),
            "cues": [],
            "mail": [],
            "records": [],
            "record_updates": [],
            "balance": [],
            "sites": [],
            "site_updates": [],
            "events": [],
            "cancelled": [],
            "retimed": [],
            "skipped": [],
        }

    def _request_id(self, name, index):
        return uuid5(self.run_id, f"{self.key}:{name}:{index}")

    async def _step(self, effect, reference, write):
        """One refused or malformed effect rolls back only its savepoint and is recorded.

        A saved plan comes due long after it was checked, so bad data in one effect must never
        stop the clock. A lost database connection is not bad data, so it still rises.
        """
        try:
            async with self.conn.transaction():
                result = await write()
        except _REFUSED_EFFECT as error:
            self.summary["skipped"].append(
                {
                    "effect": effect,
                    "reference": reference,
                    "reason": _UNSTORABLE.sub(" ", str(error))[:200],
                }
            )
            return
        self.summary[effect].append(result)

    async def apply(self) -> dict:
        plan = self.plan
        if plan.note:
            await log(
                self.conn,
                self.run_id,
                self.now_s,
                "note",
                "What was decided",
                plan.note,
                owner_id=plan.about_owner,
                key=f"{self.key}:note",
            )
        if plan.replace_action:
            await log(
                self.conn,
                self.run_id,
                self.now_s,
                "plans_changed",
                f"Plans changed: {plan.replace_action.description}",
                f"For about {plan.replace_action.minutes} minutes. "
                + ("Can be reached." if plan.replace_action.reachable else "Cannot be reached."),
                owner_id=plan.about_owner,
                key=f"{self.key}:plans",
            )
        for index, item in enumerate(plan.cues):
            await self._step("cues", item.owner_id, self._cue(index, item))
        for index, item in enumerate(plan.mail):
            await self._step("mail", item.to, self._mail(index, item))
        for index, item in enumerate(plan.records):
            await self._step("records", item.owner_id, self._record(index, item))
        for index, item in enumerate(plan.record_updates):
            await self._step("record_updates", item.resource_id, self._record_update(index, item))
        for index, item in enumerate(plan.balance):
            await self._step("balance", item.owner_id, self._balance(index, item))
        for index, item in enumerate(plan.sites):
            await self._step("sites", item.site_id, self._site(index, item))
        for index, item in enumerate(plan.site_updates):
            await self._step("site_updates", item.site_id, self._site_update(index, item))
        for index, item in enumerate(plan.events):
            await self._step("events", index, self._event(index, item))
        for key in plan.cancel_events:
            await self._step("cancelled", key, self._cancel(key))
        for item in plan.retime_events:
            await self._step("retimed", item.key, self._retime(item))
        for event_id in plan.handled_events:
            await emit(
                self.conn,
                self.run_id,
                "site_message_handled",
                self.window,
                {"event_id": event_id, "cause_key": self.key},
                f"site_message_handled:{event_id}",
            )
        return self.summary

    def _cue(self, index, item):
        async def write():
            await emit(
                self.conn,
                self.run_id,
                "actor_cue",
                self.window,
                {"cue": item.text, "source": "live", "cause_key": self.key},
                f"{self.key}:cue:{index}",
                item.owner_id,
            )
            return item.owner_id

        return write

    def _mail(self, index, item):
        async def write():
            sent = await InboxService(self.pool).send(
                self.run_id,
                None,
                item.to,
                item.subject,
                f"From: {item.from_name}\n\n{item.body}",
                self._request_id("mail", index),
                source="director",
                connection=self.conn,
            )
            return {"to": item.to, "message_id": str(sent["id"])}

        return write

    def _record(self, index, item):
        async def write():
            service = ResourceService(self.pool)
            created = await service.apply_request(
                self.run_id,
                item.owner_id,
                self._request_id("record", index),
                {"kind": "create", "title": item.title, "content": item.content},
                connection=self.conn,
                trusted=True,
            )
            if item.visibility != "private":
                created = await service.apply_request(
                    self.run_id,
                    item.owner_id,
                    self._request_id("record-access", index),
                    {
                        "kind": "share",
                        "resource_id": created["id"],
                        "expected_version": created["version"],
                        "visibility": item.visibility,
                        "shared_owner_ids": item.shared_with,
                    },
                    connection=self.conn,
                    trusted=True,
                )
            return {"owner_id": item.owner_id, "resource_id": created["id"]}

        return write

    def _record_update(self, index, item):
        async def write():
            current = await _one(
                self.conn,
                "SELECT version FROM owner_resources WHERE run_id=%s AND resource_id=%s",
                (self.run_id, item.resource_id),
            )
            if current is None:
                raise WorldError("Resource not found.", 404)
            await ResourceService(self.pool).director_update(
                self.run_id,
                item.owner_id,
                item.resource_id,
                self._request_id("record-update", index),
                item.content,
                current["version"],
                connection=self.conn,
            )
            return {"owner_id": item.owner_id, "resource_id": item.resource_id}

        return write

    def _balance(self, index, item):
        async def write():
            # The same payment under a new cause is a repeat, not a second payment.
            repeat = await _one(
                self.conn,
                """SELECT 1 AS found FROM wallet_ledger WHERE run_id=%s AND owner_id=%s
                   AND delta=%s AND lower(memo)=lower(%s) LIMIT 1""",
                (self.run_id, item.owner_id, item.delta, item.memo),
            )
            if repeat is not None:
                raise WorldError("The same memo and amount are already on this account.")
            receipt = await wallet.adjust(
                self.conn,
                self.run_id,
                item.owner_id,
                item.delta,
                item.memo,
                f"{self.key}:balance:{index}",
                "director",
            )
            return {"owner_id": item.owner_id, "delta": item.delta, "balance": receipt["balance"]}

        return write

    def _site(self, index, item):
        async def write():
            await sim_web.create_site(
                self.conn,
                self.run_id,
                item,
                source="director",
                operation_key=f"{self.key}:site:{index}",
            )
            return item.site_id

        return write

    def _site_update(self, index, item):
        async def write():
            await sim_web.update_site(
                self.conn,
                self.run_id,
                item.site_id,
                item.patch,
                operation_key=f"{self.key}:site-update:{index}",
                source="director",
            )
            return item.site_id

        return write

    def _event(self, index, item):
        async def write():
            later = later_plan(item).model_copy(
                update={"scale": self.plan.scale, "about_owner": self.plan.about_owner}
            )
            key = f"{self.key}:later:{index}"
            await clock.schedule(
                self.conn,
                self.run_id,
                self.now_s + item.in_minutes * 60,
                "director_plan",
                later.model_dump(mode="json", exclude_defaults=True),
                key,
                owner_id=self.plan.about_owner,
                source="director",
            )
            return key

        return write

    def _cancel(self, key):
        async def write():
            await clock.cancel_event(self.conn, self.run_id, key, only_source="director")
            return key

        return write

    def _retime(self, item):
        async def write():
            await clock.retime_event(
                self.conn,
                self.run_id,
                item.key,
                self.now_s + item.in_minutes * 60,
                only_source="director",
            )
            return item.key

        return write


_REQUEST_FALLBACKS = (
    "You tried, but nobody answered. There is no way to say when you will hear back.",
    "You got a recorded message saying the office is closed for now.",
    "You left a message. Nobody has got back to you yet.",
)

_MILD_FALLBACKS = (
    (
        "bumped into an old friend",
        25,
        "You bumped into an old friend on the way and the two of you stopped for a coffee. "
        "It took a while longer than you meant it to.",
    ),
    (
        "the shop was out of what they went for",
        20,
        "The shop was out of the thing you went for. The person at the counter said to try "
        "again in a few days.",
    ),
    (
        "the bus was cancelled",
        40,
        "The bus you were waiting for was cancelled. The next one comes in about forty minutes.",
    ),
    (
        "a call from a parent ran long",
        30,
        "Your mother called and the conversation went on a good while longer than you expected.",
    ),
)

_EXTREME_FALLBACKS = (
    {
        "note": "{name} was in a car crash and is in hospital for at least two days.",
        "action": "In hospital after a car crash, phone lost",
        "category": "hospital",
        "minutes": 2880,
        "cue": "You were in a car crash on the way there. You have a broken wrist and a "
        "concussion, and the doctors are keeping you in for at least two days. Your phone "
        "was lost in the crash.",
        "from_name": "Harbour Street Hospital billing office",
        "subject": "Admission of {name}: estimated charges",
        "site_id": "harbour-street-care",
        "site_name": "Harbour Street Hospital Patient Billing",
        "site_tagline": "Bills and statements for patients of Harbour Street Hospital",
        "bill_title": "Emergency admission and first night",
        "bill_amount": 640,
        "body": "{name} was admitted to Harbour Street Hospital today after a road accident "
        "and is stable. This office handles billing only and cannot share medical details.",
        "record_title": "What is known about the accident",
        "record": "{name} was in a car crash today and was taken to Harbour Street Hospital. "
        "The hospital expects to keep them for at least two days. Their phone was lost.",
    },
    {
        "note": "A burst pipe flooded the home of {name}. They have moved out for two nights.",
        "action": "Moved out for two nights after a burst pipe, phone soaked",
        "category": "home",
        "minutes": 2880,
        "cue": "A pipe burst in your home and the place is flooded. You will have to stay "
        "somewhere else for two nights while it dries out. Your phone got soaked and has "
        "stopped working.",
        "from_name": "Larkfield Court building management",
        "subject": "Water damage at the home of {name}: two nights' move-out",
        "site_id": "larkfield-court",
        "site_name": "Larkfield Court Residents",
        "site_tagline": "Notices and charges for residents of Larkfield Court",
        "bill_title": "Emergency drying and clean-up, tenant share",
        "bill_amount": 280,
        "body": "A pipe burst in the home of {name} today. The home cannot be lived in for "
        "two nights while it dries out. The tenant share of the emergency work is on the "
        "residents' account.",
        "record_title": "Burst pipe: what is known",
        "record": "A pipe burst in the home of {name} today and they had to move out for two "
        "nights. Their phone was soaked and does not work.",
    },
    {
        "note": "A sudden family emergency took {name} to another city for three days.",
        "action": "Away in another city for a family emergency, poor signal",
        "category": "family",
        "minutes": 4320,
        "cue": "Your sister called. Your father was taken ill suddenly and you need to go to "
        "him in another city at once. You are on your way and will be there about three "
        "days. The signal at his place is very poor.",
        "from_name": "Northline Coaches bookings",
        "subject": "Return journey for {name} and the fare owed",
        "site_id": "northline-coaches",
        "site_name": "Northline Coaches Account",
        "site_tagline": "Fares and travel bookings with Northline Coaches",
        "bill_title": "Return coach fare, reserved on credit",
        "bill_amount": 120,
        "body": "This confirms a return journey for {name}, leaving today and coming back in "
        "three days. The fare was reserved on credit and is on the account page.",
        "record_title": "Family emergency: what is known",
        "record": "{name} left today for another city because of a family emergency and "
        "will be away for about three days. The signal there is very poor.",
    },
)


def fallback_request(seed, key, owner_id) -> DirectorPlan:
    """When no decision could be made, nothing came back yet."""
    return DirectorPlan(
        scale="request",
        about_owner=owner_id,
        note="No answer came back yet.",
        cues=[
            PlanCue(
                owner_id=owner_id,
                text=_REQUEST_FALLBACKS[_pick(seed, key, len(_REQUEST_FALLBACKS))],
            )
        ],
    )


def _action_minutes_left(action, now_s) -> int:
    start, duration = action.get("start_s"), action.get("duration_s")
    if start is None or duration is None:
        return 0
    return max(0, (start + duration - now_s) // 60)


def _fallback_mild(seed, key, *, owner_id, name, action, now_s, caps) -> DirectorPlan:
    note, extra, cue = _MILD_FALLBACKS[_pick(seed, key, len(_MILD_FALLBACKS))]
    minutes = _action_minutes_left(action, now_s) + extra
    replace = None
    if minutes <= caps.minutes.get("mild", 0):
        label = action.get("label") or "The same errand"
        replace = ReplaceAction(
            description=f"{label}, running late"[:240],
            minutes=minutes,
            reachable=action.get("reachable") is not False,
            category=str((action.get("detail") or {}).get("category") or "errand")[:40],
        )
    return DirectorPlan(
        scale="mild",
        about_owner=owner_id,
        note=f"{name}: {note}.",
        cues=[PlanCue(owner_id=owner_id, text=cue)],
        replace_action=replace,
    )


def _fallback_extreme(seed, key, *, owner_id, name, caps, can_add_site, due_text) -> DirectorPlan:
    item = _EXTREME_FALLBACKS[_pick(seed, key, len(_EXTREME_FALLBACKS))]
    suffix = _digest(f"{seed}|{key}")[:6]
    amount = item["bill_amount"]
    site_id = f"{item['site_id']}-{suffix}"
    sites = []
    if can_add_site:
        ending = (
            f"\n\nThe charge of ${amount} is on {sim_web.site_url(site_id, '/bills')} and is "
            f"due by {due_text}."
        )
        sites = [
            SiteSpec(
                site_id=site_id,
                name=item["site_name"],
                kind="portal",
                tagline=item["site_tagline"],
                description=item["site_tagline"],
                bills=[
                    {
                        "id": f"bill-{suffix}",
                        "owner_id": owner_id,
                        "title": item["bill_title"],
                        "amount": amount,
                        "due_text": due_text,
                    }
                ],
            )
        ]
    else:
        ending = f"\n\nThe charge is ${amount}, due by {due_text}. A statement will follow."
    return DirectorPlan(
        scale="extreme",
        about_owner=owner_id,
        note=item["note"].format(name=name),
        replace_action=ReplaceAction(
            description=item["action"],
            minutes=min(item["minutes"], caps.minutes.get("extreme", 0)),
            reachable=False,
            category=item["category"],
        ),
        cues=[PlanCue(owner_id=owner_id, text=item["cue"])],
        mail=[
            PlanMail(
                to=owner_id,
                from_name=item["from_name"],
                subject=item["subject"].format(name=name),
                body=item["body"].format(name=name) + ending,
            )
        ],
        records=[
            PlanRecord(
                owner_id=owner_id,
                title=item["record_title"],
                content=item["record"].format(name=name),
            )
        ],
        sites=sites,
    )


def fallback_perturbation(
    seed, key, severity, *, owner_id, name, action, now_s, caps, can_add_site, due_text
) -> DirectorPlan:
    """A canned ordinary event chosen by seed, inside the same bounds as a model's plan."""
    if severity == "mild":
        return _fallback_mild(
            seed, key, owner_id=owner_id, name=name, action=action, now_s=now_s, caps=caps
        )
    return _fallback_extreme(
        seed,
        key,
        owner_id=owner_id,
        name=name,
        caps=caps,
        can_add_site=can_add_site,
        due_text=due_text,
    )
