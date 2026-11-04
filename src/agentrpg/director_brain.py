"""The live world on clocked runs: typed plans, bounded effects and a seeded fallback.

A language model writes what happens. Code validates it and applies it. Every model call
happens with no database transaction open. apply_plan commits one plan once, in one short
transaction, with effect keys derived from the cause key. The model reads no board text,
no credentials and no words that name this machinery. That includes the JSON schema, so
the plan classes carry model-facing titles and aliases. Python code uses contract names.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid5

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
from agentrpg.sim_web import SiteSpec
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


class PlanCaps(BaseModel):
    """Money moved per plan and length of a changed action, by the scale of the plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    money: dict[str, int] = {"mild": 50, "extreme": 2000, "review": 200, "request": 200, "site": 0}
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
    if len(json.dumps(value, ensure_ascii=False, default=str)) > MAX_PAYLOAD_CHARS:
        raise ValueError(f"{name} is too long.")
    return value


class PlanSiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", title="SiteChange")

    site_id: Reference
    patch: dict[str, Any]

    @field_validator("patch")
    @classmethod
    def small(cls, value):
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
        return {"changes": "plan", "news": "cue"}.get(value, value)

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

    @model_validator(mode="after")
    def check_later_happenings(self) -> Self:
        for event in self.events:
            later_plan(event)
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


# Fields that are references or settings rather than text a person reads.
_REFERENCE_FIELDS = frozenset(
    {
        "owner_id",
        "to",
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
        raise PlanRejected(f"The money moved in one answer must total at most {money_cap}.")
    if plan.replace_action:
        minutes_cap = caps.minutes.get(plan.scale, 0)
        if minutes_cap == 0:
            raise PlanRejected("Leave replace_action out here. Nobody's plans change right now.")
        if plan.replace_action.minutes > minutes_cap:
            raise PlanRejected(f"replace_action must last at most {minutes_cap} minutes.")
    if not check_answer:
        return
    if plan.scale == "mild" and plan.sites:
        raise PlanRejected("Something this small needs no new website. Leave sites out.")
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

How to fill the answer. Every part is optional, so leave out what this moment does not need:
- note: a line or two for your own memory of what you decided.
- news: what one person privately notices or learns right now, written to them as "you".
- replace_action: what the person is doing instead from now on, how many minutes it takes, and \
whether they can be reached by phone or message meanwhile.
- mail: a new email in a person's mailbox from a believable sender. Put the sender in from_name.
- records: a new document in a person's files, such as what is known so far.
- record_updates: new full text for a document listed in files.
- balance: money into (positive) or out of (negative) a person's account in whole dollars, with a \
short memo.
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
Keep it small. No injury, no crisis, no large bill, no new website. Usually news for this person \
is enough, plus replace_action if it costs them time, counting the time left on what they were \
doing.
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
less than hoped, answers can come later through events. If money changes hands, add it to balance.
"""
)

SITE_PROMPT = (
    COMMON_PROMPT
    + """
Someone just searched the web for searched_for and no known site fits. Build one invented website \
that could plausibly exist for this search, in one entry of sites, with believable prices and \
contents. Add nothing else. If no such site could plausibly exist, return an empty answer.
"""
)

REVIEW_PROMPT = (
    COMMON_PROMPT
    + """
Time has passed. waiting lists what people or their assistants sent to websites: questions, \
applications and items listed for sale. Decide what comes back for each one, in a believable \
market. Not everything sells. Prices below asking are common. Replies take time and some never \
come. When something sells, use events with in_minutes for when it happens, and put in its payload \
the buyer's mail, the agreed price in balance, and site_updates with a patch such as \
{"listings": [{"id": "the listing_id", "status": "sold"}]}. An application is answered the same \
way with {"applications": [{"id": "the application_id", "status": "accepted"}]} or "declined". \
Reply as the site or the buyer would.
Do not change anybody's plans here.
If add_small_happenings is true, also do what setting asks for between times: one or two small \
everyday happenings, and money going out or coming in for what people really do. If setting asks \
for nothing of the kind, or add_small_happenings is false, add nothing beyond answers to waiting.
"""
)

# Plain-word detection, so a review with nothing waiting can skip its model call. The list is
# wide on purpose: a wrong yes costs one cheap call, and a wrong no leaves the world standing still.
_HAPPENINGS_ASKED = re.compile(
    r"\b(happenings?|small events?|everyday events?|ordinary events?|daily life|everyday life|"
    r"day to day|now and then|from time to time|sometimes|occasional\w*|ambient|background life|"
    r"local news|what may vary|take money off|money off|daily expenses|living costs)\b",
    re.I,
)
_MESSAGE_HIDDEN = re.compile(
    r"(^|_)(request|operation|key|hash|window|run|sim|version|token)(_|$)", re.I
)


def notes_ask_for_happenings(notes: str | None) -> bool:
    """Whether the author's setting notes ask for small world happenings during reviews."""
    return bool(notes and _HAPPENINGS_ASKED.search(notes))


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
    """The readable part of a site_message payload."""
    return {
        key: value
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
                """SELECT site_id,name,kind,spec->>'tagline' AS tagline FROM web_sites
                   WHERE run_id=%s ORDER BY site_id""",
                (run_id,),
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
            "open_sites": sites,
            "files": files,
            "upcoming": [
                {
                    "ref": refs_by_key[item["key"]],
                    "when": moment.day_and_time(item["due_s"]),
                    "what": _later_summary(item["payload"]),
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

    def _finish(self, plan: DirectorPlan, moment: _Moment, scale, owner_id, handled, keep_reach):
        """Set code-owned fields and check everything that depends on this moment."""
        if keep_reach and plan.replace_action and not plan.replace_action.reachable:
            raise PlanRejected("Something this small leaves them reachable. Set reachable true.")
        unknown = [ref for ref in plan.cancel_events if ref not in moment.refs]
        unknown += [item.key for item in plan.retime_events if item.key not in moment.refs]
        if unknown:
            raise PlanRejected("cancel_events and retime_events must use a ref from upcoming.")
        for item in plan.record_updates:
            if item.resource_id not in moment.resource_ids:
                raise PlanRejected("record_updates must use a resource_id listed in files.")
        for item in plan.site_updates:
            if item.site_id not in moment.site_ids:
                raise PlanRejected("site_updates must use a site_id listed in open_sites.")
        if plan.sites and len(moment.site_ids) + len(plan.sites) > moment.max_sites:
            raise PlanRejected("No more websites can be added. Leave sites out.")
        result = plan.model_copy(
            update={
                "scale": scale,
                "about_owner": owner_id,
                "handled_events": list(handled),
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

    async def _decide(self, run_id, prompt, moment, data, *, scale, owner_id, handled, keep_reach):
        """Return (plan, []), or (None, failures) when a repair attempt also failed.

        That makes at most three model calls: first answer, one look-up, one repair.
        A budget refusal is not a failure of the decision. It rises to the caller.
        """
        looked_up = False
        repaired = False
        failures: list[str] = []
        answer: dict = {}

        def remember(candidate):
            answer["last"] = candidate

        while True:
            problem = None
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
                if plan.need_more and not looked_up:
                    looked_up = True
                    data = {
                        **data,
                        "looked_up": await self._look_up(run_id, moment, plan.need_more),
                    }
                    continue
                return self._finish(plan, moment, scale, owner_id, handled, keep_reach), []
            except ModelGenerationError as error:
                if error.kind == "budget":
                    raise
                failures.append(error.kind)
            except StructuredOutputError as error:
                failures.append(error.kind)
                problem = str(error)
            except ValidationError as error:
                failures.append("invalid_answer")
                problem = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                    for item in error.errors()[:8]
                )
            except PlanRejected as error:
                failures.append("rejected_answer")
                problem = str(error)
            if repaired:
                return None, failures
            repaired = True
            if problem:
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
        handled=(),
        skip=None,
        keep_reach=False,
    ):
        run_id = _run_id(run)
        moment = await self._gather(run_id, now_s, query, owner_id=owner_id)
        if not moment.enabled or (skip is not None and skip(moment)):
            return DirectorPlan(scale=scale, about_owner=owner_id)
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
            handled=handled,
            keep_reach=keep_reach,
        )
        if plan is not None:
            return plan
        plan = fallback(moment)
        validate_plan(plan, owners=moment.names, caps=self.caps, check_answer=False)
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

    async def respond_to_request(self, run, owner_id, text, now_s) -> DirectorPlan:
        """Decide what the world says to something an owner tries to make happen."""
        text = (text or "").strip()[:1000]
        key = f"request:{owner_id}:{now_s}:{_digest(text)[:16]}"
        return await self._think(
            run,
            now_s,
            prompt=REQUEST_PROMPT,
            scale="request",
            trigger={"tried_by": owner_id, "attempt": text},
            query=text,
            key=key,
            owner_id=owner_id,
            fallback=lambda moment: fallback_request(moment.record["seed"], key, owner_id),
        )

    async def site_for_search(self, run, owner_id, query, now_s) -> DirectorPlan:
        """Build one site for a search with no match, unless the site limit is reached."""
        query = (query or "").strip()[:300]
        return await self._think(
            run,
            now_s,
            prompt=SITE_PROMPT,
            scale="site",
            trigger={"searched_by": owner_id, "searched_for": query},
            query=query,
            key=f"site:{owner_id}:{now_s}:{_digest(query)[:16]}",
            owner_id=owner_id,
            fallback=lambda _moment: DirectorPlan(scale="site", about_owner=owner_id),
            skip=lambda moment: not moment.can_add_site,
        )

    async def review(self, run, now_s, reason) -> DirectorPlan:
        """Answer site messages. With none, and no happenings asked for, this costs no call."""
        run_id = _run_id(run)
        async with self.pool.connection() as conn:
            record = await _one(
                conn, "SELECT scenario FROM environment_runs WHERE id=%s", (run_id,)
            )
            state = await _one(conn, "SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
            if record is None or state is None:
                raise DirectorPlanError("This run has no clock.")
            waiting = await _rows(
                conn,
                """SELECT e.id,e.owner_id,e.payload,e.created_at FROM run_events e
                   WHERE e.run_id=%s AND e.kind='site_message' AND NOT EXISTS (
                       SELECT 1 FROM run_events d WHERE d.run_id=e.run_id
                       AND d.operation_key='site_message_handled:'||e.id::text)
                   ORDER BY e.created_at,e.id LIMIT %s""",
                (run_id, MAX_WAITING),
            )
        settings = _section(record, state).get("director") or {}
        notes = settings.get("setting_notes")
        happenings = notes_ask_for_happenings(notes)
        if settings.get("enabled") is False or not (waiting or happenings):
            return DirectorPlan(scale="review")
        zone = _zone(record, state)
        details = [
            {
                "from": item["owner_id"],
                "when": clock.format_time(item["created_at"], zone),
                **_message_details(item["payload"]),
            }
            for item in waiting
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
            handled=[str(item["id"]) for item in waiting],
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
        """One refused effect rolls back only its savepoint and is recorded."""
        try:
            async with self.conn.transaction():
                result = await write()
        except (InboxError, WorldError, clock.ClockError) as error:
            self.summary["skipped"].append(
                {"effect": effect, "reference": reference, "reason": str(error)[:200]}
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
