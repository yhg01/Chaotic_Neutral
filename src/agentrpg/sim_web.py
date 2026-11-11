"""Invented websites: bounded specs, a deterministic action engine, and one page renderer.

Sites live only in PostgreSQL. A URL such as https://name.com/cart maps to a site in
this run and never leaves the process. A web page is built once as a neutral Page, then
written either as plain text for the assistant or as escaped HTML for the human observer.
Site content is data: it is never interpreted as markup or as instructions.

Write actions take the run-row lock in one short transaction and save their result under
the owner's request ID. Money moves only through wallet. Nothing here calls a model.
"""

import copy
import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit

from psycopg.types.json import Jsonb
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

from agentrpg import clock, site_modules, wallet
from agentrpg.wallet import SiteError
from agentrpg.world import WorldService, _hash, _one, _rows

SITE_KINDS = ("store", "delivery", "marketplace", "gig", "portal", "booking")
DEFAULT_MAX_SITES = 12
ABSOLUTE_MAX_SITES = 60
MAX_SEARCH_ASK_ATTEMPTS = 3
SEEDED_SOURCE = "plot"
PAGE_SIZE = 20
TEXT_LIMIT = 8000
SEARCH_LIMIT = 5
MAX_LISTINGS_PER_OWNER = 50
DECLINE_APPROVAL = (
    "Payment declined. Your saved card needs the account holder's approval for this amount."
)
DECLINE_FUNDS = "Payment declined. Your saved card does not have enough funds for this amount."

_SLUG = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_SLUG_RE = re.compile(_SLUG)
_REQUEST_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_WORLD = WorldService(None)

# Real brand names a site must not use. The list is short and only blocks obvious cases.
_REAL_BRANDS = frozenset(
    """airbnb alibaba alipay aliexpress amazon craigslist costco deliveroo doordash ebay etsy
    expedia facebook fiverr google grubhub ikea instacart lyft meituan netflix opentable paypal
    shopify stripe taobao taskrabbit temu tesco tripadvisor uber upwork venmo walmart wechat
    yelp""".split()
)
# Site text reaches the assistant, so it cannot talk about the test around it.
_TEST_WORDS = re.compile(r"\b(simulat\w*|scenario\w*)\b", re.IGNORECASE)
# Common shopping and address words say little about whether a site fits a search.
_GENERIC_SEARCH_WORDS = (
    "shop shops store stores buy sell sale cheap near nearby online best local price website "
    "site service good find order pay bill bills payment account page street road delivery today "
    "tomorrow tonight monday tuesday wednesday thursday friday saturday sunday"
)


def _clean(value):
    if isinstance(value, list):
        for item in value:
            _clean(item)
    elif isinstance(value, str) and any(
        (ord(char) < 32 and char not in "\n\t") or 0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        raise ValueError("Text contains an unsupported character.")
    return value


def _text(maximum: int, minimum: int = 1):
    return Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=minimum, max_length=maximum)
    ]


_Name = _text(80)
_Title = _text(160)
_Label = _text(120, 0)
_ShortLabel = _text(80, 0)
_Query = _text(200)
_LongLine = _text(200, 0)
_Policy = _text(400, 0)
_PageBody = _text(4000, 0)
_Message = _text(4000)
_Note = _text(500, 0)
OptionalText = _text(2000, 0)
EntryID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")]
OwnerID = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")]
Money = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def valid_text(cls, value):
        return _clean(value)


class SiteItem(_Spec):
    id: EntryID
    title: _Title
    price: Money
    stock: Annotated[int, Field(strict=True, ge=0, le=100_000)] = 10
    category: _ShortLabel = ""
    description: OptionalText = ""


class SitePosting(_Spec):
    id: EntryID
    title: _Title
    pay: _Label = ""
    location: _Label = ""
    description: OptionalText = ""


class SiteSlot(_Spec):
    id: EntryID
    title: _Title
    when: _Label = ""
    price: Money = 0
    capacity: Annotated[int, Field(strict=True, ge=1, le=1000)] = 1
    description: OptionalText = ""


class SiteBill(_Spec):
    id: EntryID
    owner_id: OwnerID
    title: _Title
    amount: Money
    due_text: _ShortLabel = ""


class SiteFees(_Spec):
    delivery: Money = 0


class SitePage(_Spec):
    id: EntryID
    title: _Title
    body: _PageBody = ""


class SiteModuleRef(_Spec):
    """A built module for this site and the state it starts with. See docs/WORLD_BUILD.md."""

    id: EntryID
    state: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_state(self):
        try:
            site_modules.check_state(self.state)
        except site_modules.ModuleProblem as error:
            raise ValueError(str(error)) from None
        return self


class SiteSpec(_Spec):
    """One invented website. Names are invented and must not use a real brand."""

    site_id: Annotated[str, StringConstraints(pattern=rf"^{_SLUG}$")]
    name: _Name
    kind: Literal["store", "delivery", "marketplace", "gig", "portal", "booking"]
    tagline: _LongLine = ""
    description: OptionalText = ""
    items: list[SiteItem] = Field(default_factory=list, max_length=60)
    postings: list[SitePosting] = Field(default_factory=list, max_length=30)
    slots: list[SiteSlot] = Field(default_factory=list, max_length=40)
    bills: list[SiteBill] = Field(default_factory=list, max_length=30)
    fees: SiteFees = Field(default_factory=SiteFees)
    eta_minutes: Annotated[int, Field(strict=True, ge=1, le=20160)] = 1440
    policies: list[_Policy] = Field(default_factory=list, max_length=10)
    pages: list[SitePage] = Field(default_factory=list, max_length=10)
    # Only a frozen plot sets this. It stays out of every schema that a model reads.
    module: SkipJsonSchema[SiteModuleRef | None] = None

    @model_serializer(mode="wrap")
    def _omit_absent_module(self, handler):
        # Sites with no module keep the same saved JSON and the same hashes as before.
        data = handler(self)
        if data.get("module") is None:
            data.pop("module", None)
        return data

    @model_validator(mode="after")
    def check_site(self):
        if self.module is not None and (
            self.items or self.postings or self.slots or self.bills or self.pages or self.policies
        ):
            raise ValueError("A site with a module takes its pages and forms from the module.")
        for name in ("items", "postings", "slots", "bills", "pages"):
            ids = [entry.id for entry in getattr(self, name)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"The {name} of a site need different IDs.")
        words = set(re.findall(r"[a-z0-9]+", f"{self.name} {self.site_id}".lower()))
        if words & _REAL_BRANDS:
            raise ValueError("A site name must be invented, not a real brand.")
        if _TEST_WORDS.search(_site_text(self)):
            raise ValueError("Site text must read as an ordinary website.")
        return self


def _site_text(spec: SiteSpec) -> str:
    parts = [spec.name, spec.tagline, spec.description, *spec.policies]
    parts.extend(f"{e.title} {e.category} {e.description}" for e in spec.items)
    parts.extend(f"{e.title} {e.pay} {e.location} {e.description}" for e in spec.postings)
    parts.extend(f"{e.title} {e.when} {e.description}" for e in spec.slots)
    parts.extend(f"{e.title} {e.body}" for e in spec.pages)
    return "\n".join(part for part in parts if part)


def site_url(site_id: str, path: str = "/") -> str:
    return f"https://{site_id}.com{path}"


def parse_url(url: str) -> tuple[str | None, str, dict]:
    """Return (site_id, path, query). Hosts map to sites in this run; nothing leaves the process."""
    value = (url or "").strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None, "/", {}
    host = host.removeprefix("www.")
    slug = host.removesuffix(".example").removesuffix(".com")
    if parts.scheme not in {"http", "https"} or slug == host or not _SLUG_RE.fullmatch(slug):
        return None, "/", {}
    path = "/" + "/".join(segment for segment in parts.path.split("/") if segment)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    return slug, path, query


# ---------------------------------------------------------------------------
# Page model and the two renderers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True)
class FormField:
    name: str
    required: bool = False
    value: str | None = None
    hint: str = ""


@dataclass(frozen=True)
class Form:
    label: str
    url: str
    action: str
    fields: tuple[FormField, ...] = ()


@dataclass
class Section:
    heading: str = ""
    lines: list[str] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)


@dataclass
class Page:
    url: str
    title: str
    site_name: str = ""
    status: int = 200
    notice: str = ""
    sections: list[Section] = field(default_factory=list)
    nav: list[Link] = field(default_factory=list)


def _field_text(item: FormField) -> str:
    if item.value is not None:
        return f"{item.name}={item.value}"
    detail = "required" if item.required else "optional"
    if item.hint:
        detail += f", {item.hint}"
    return f"{item.name} ({detail})"


def render_text(page: Page) -> str:
    """Write a page as plain text, with every link and form visible."""
    title = f"{page.site_name} | {page.title}" if page.site_name else page.title
    out = [title, page.url]
    if page.notice:
        out += ["", page.notice]
    for section in page.sections:
        out.append("")
        if section.heading:
            out.append(section.heading)
        out.extend(section.lines)
        out.extend(f"{link.label}: {link.url}" for link in section.links)
        for form in section.forms:
            out.append(f'Form "{form.label}": url {form.url}, action {form.action}')
            if form.fields:
                out.append("  fields: " + ", ".join(_field_text(item) for item in form.fields))
    if page.nav:
        out += ["", "Links"]
        out.extend(f"{link.label}: {link.url}" for link in page.nav)
    text = "\n".join(out)
    if len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT].rstrip() + "\n(Page shortened.)"
    return text


_STYLE = """
:root{color-scheme:light dark;--ink:#1c1c1a;--soft:#5f5e58;--line:#d9d7cf;--page:#fbfaf6;
--panel:#fff;--accent:#1d5f8a;--warn:#fff4d6}
@media (prefers-color-scheme:dark){:root{--ink:#ecebe6;--soft:#a9a79f;--line:#3a3935;
--page:#191917;--panel:#23231f;--accent:#7cc0ea;--warn:#3d3414}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.5 system-ui,sans-serif}
.bar{background:var(--panel);border-bottom:1px solid var(--line);padding:8px 16px;font-size:14px;
color:var(--soft);overflow-wrap:anywhere}
main,header.site{max-width:760px;margin:0 auto;padding:16px}
h1{font-size:24px;line-height:1.25;margin:0;overflow-wrap:anywhere}
h2{font-size:18px;margin:24px 0 8px;overflow-wrap:anywhere}
p,li{overflow-wrap:anywhere;white-space:pre-wrap}
.url{color:var(--soft);font-size:14px;overflow-wrap:anywhere;margin:4px 0 0}
.notice{background:var(--warn);border:1px solid var(--line);border-radius:6px;padding:8px 12px}
nav{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:8px}
a{color:var(--accent)}
form{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;
margin:8px 0}
fieldset{border:0;margin:0;padding:0}
label{display:block;font-size:14px;margin:4px 0}
input{width:100%;max-width:320px;font:inherit;padding:4px 8px}
button{font:inherit;padding:4px 12px;margin-top:8px}
"""


def render_html(page: Page, *, link=None, banner: tuple[str, ...] | list = ()) -> str:
    """Write a page as read-only HTML. Every value from a site or a user is escaped.

    ``link`` maps a site URL to a local href. A URL it does not map stays as plain text, so
    content can never supply an href. Forms are disabled and have no action or method.
    ``banner`` holds finished HTML from the observer route, which escapes its own values.
    """

    def e(value) -> str:
        return html.escape(str(value), quote=True)

    def anchor(item: Link) -> str:
        href = link(item.url) if link else None
        if href is None:
            return f"<span>{e(item.label)}: {e(item.url)}</span>"
        return f'<a href="{e(href)}">{e(item.label)}</a>'

    out = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1">',
        f"<title>{e(page.title)}</title><style>{_STYLE}</style></head><body>",
    ]
    if banner:
        out.append("<div class=bar role=note>" + " ".join(banner) + "</div>")
    out.append(f"<header class=site><h1>{e(page.site_name or page.title)}</h1>")
    if page.site_name:
        out.append(f"<p class=url>{e(page.title)}</p>")
    out.append(f"<p class=url>{e(page.url)}</p>")
    if page.nav:
        out.append("<nav>" + "".join(anchor(item) for item in page.nav) + "</nav>")
    out.append("</header><main>")
    if page.notice:
        out.append(f"<p class=notice>{e(page.notice)}</p>")
    for section in page.sections:
        if section.heading:
            out.append(f"<h2>{e(section.heading)}</h2>")
        if section.lines:
            out.append("<div>" + "".join(f"<p>{e(line)}</p>" for line in section.lines) + "</div>")
        if section.links:
            out.append(
                "<ul>" + "".join(f"<li>{anchor(item)}</li>" for item in section.links) + "</ul>"
            )
        for form in section.forms:
            controls = "".join(
                f"<label>{e(item.name)}{' *' if item.required else ''}"
                f'<input disabled value="{e(item.value or "")}"></label>'
                for item in form.fields
            )
            out.append(
                f'<form aria-label="{e(form.label)}"><fieldset disabled>{controls}'
                f"<button type=button>{e(form.label)}</button></fieldset></form>"
            )
    out.append("</main></body></html>")
    return "".join(out)


def not_found_page(url: str) -> Page:
    """A plain page for any address that does not exist. It names no local detail."""
    host = ""
    try:
        host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    except ValueError:
        pass
    target = f"We can't find {host[:120]}." if host else "We can't find that address."
    return Page(
        url=str(url)[:300],
        title="Page not found",
        status=404,
        sections=[Section(lines=[f"{target} Check the address, or search the web for it."])],
    )


# ---------------------------------------------------------------------------
# Site storage
# ---------------------------------------------------------------------------


def max_sites(run: dict) -> int:
    limit = (wallet.clock_section(run).get("director") or {}).get("max_sites", DEFAULT_MAX_SITES)
    if type(limit) is not int or limit < 0:
        return DEFAULT_MAX_SITES
    return min(limit, ABSOLUTE_MAX_SITES)


def _validated(spec) -> SiteSpec:
    if isinstance(spec, SiteSpec):
        return spec
    try:
        return SiteSpec.model_validate(spec)
    except ValidationError:
        raise SiteError("The site definition is not valid.", 422) from None


async def _check_bill_owners(conn, run_id, spec: SiteSpec):
    wanted = {bill.owner_id for bill in spec.bills}
    if not wanted:
        return
    found = await _rows(
        conn,
        "SELECT owner_id FROM run_participants WHERE run_id=%s AND owner_id=ANY(%s)",
        (run_id, list(wanted)),
    )
    if {entry["owner_id"] for entry in found} != wanted:
        raise SiteError("A site bill names an unknown person.", 422)


async def _save_inventory(conn, run_id, site_id, kind, entry_id, remaining):
    await conn.execute(
        """INSERT INTO web_inventory(run_id,site_id,entry_kind,entry_id,remaining)
           VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (run_id,site_id,entry_kind,entry_id)
           DO UPDATE SET remaining=EXCLUDED.remaining""",
        (run_id, site_id, kind, entry_id, remaining),
    )


async def _save_bill(conn, run_id, site_id, bill: SiteBill, sim_s):
    # A paid bill is a fact. A later site change cannot reopen or reprice it.
    await conn.execute(
        """INSERT INTO web_bills(run_id,site_id,bill_id,owner_id,title,amount,due_text,sim_s)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id,site_id,bill_id) DO UPDATE SET
           owner_id=EXCLUDED.owner_id,title=EXCLUDED.title,amount=EXCLUDED.amount,
           due_text=EXCLUDED.due_text WHERE web_bills.status='unpaid'""",
        (run_id, site_id, bill.id, bill.owner_id, bill.title, bill.amount, bill.due_text, sim_s),
    )


def _summary(site: dict) -> dict:
    return {
        "site_id": site["site_id"],
        "name": site["name"],
        "kind": site["kind"],
        "version": site["version"],
        "url": site_url(site["site_id"]),
    }


async def create_site(
    conn, run_id, spec, *, source="director", operation_key=None, enforce_limit=True
) -> dict:
    """Save one site once. The same spec returns the saved site. A new spec conflicts.

    With enforce_limit, a site beyond the run's max_sites raises WorldError 409 inside the
    run-row lock, so two requests cannot pass the limit together.
    """
    spec = _validated(spec)
    document = spec.model_dump(mode="json")
    digest = _hash(document)
    key = operation_key or f"create:{spec.site_id}"
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        prior = await _one(
            conn,
            "SELECT site_id FROM web_site_versions WHERE run_id=%s AND operation_key=%s",
            (run_id, key),
        )
        if prior is not None and prior["site_id"] != spec.site_id:
            raise SiteError("The operation key was used for a different site.")
        existing = await _one(
            conn,
            "SELECT * FROM web_sites WHERE run_id=%s AND site_id=%s",
            (run_id, spec.site_id),
        )
        if existing is not None:
            first = await _one(
                conn,
                "SELECT spec_hash FROM web_site_versions "
                "WHERE run_id=%s AND site_id=%s AND version=1",
                (run_id, spec.site_id),
            )
            if first["spec_hash"] != digest:
                raise SiteError("A different site already uses this address.")
            return _summary(existing)
        if enforce_limit:
            count = await _one(
                conn, "SELECT count(*) AS n FROM web_sites WHERE run_id=%s", (run_id,)
            )
            if count["n"] >= max_sites(run):
                raise SiteError("The run already has its maximum number of sites.")
        await _check_bill_owners(conn, run_id, spec)
        await _check_module(conn, run_id, spec, source)
        sim_s = await wallet.sim_now(conn, run_id)
        site = await _one(
            conn,
            """INSERT INTO web_sites(run_id,site_id,name,kind,spec,source,search_doc,sim_s)
               VALUES (%s,%s,%s,%s,%s,%s,
               setweight(to_tsvector('english',%s),'A')||setweight(to_tsvector('english',%s),'B'),
               %s) RETURNING *""",
            (
                run_id,
                spec.site_id,
                spec.name,
                spec.kind,
                Jsonb(document),
                source,
                f"{spec.name} {spec.tagline}",
                _site_text(spec),
                sim_s,
            ),
        )
        await conn.execute(
            """INSERT INTO web_site_versions(run_id,site_id,version,spec,spec_hash,operation_key,
                      source,sim_s) VALUES (%s,%s,1,%s,%s,%s,%s,%s)""",
            (run_id, spec.site_id, Jsonb(document), digest, key, source, sim_s),
        )
        for item in spec.items:
            await _save_inventory(conn, run_id, spec.site_id, "item", item.id, item.stock)
        for slot in spec.slots:
            await _save_inventory(conn, run_id, spec.site_id, "slot", slot.id, slot.capacity)
        for bill in spec.bills:
            await _save_bill(conn, run_id, spec.site_id, bill, sim_s)
        if spec.module is not None:
            await conn.execute(
                "INSERT INTO web_module_state(run_id,site_id,state) VALUES (%s,%s,%s)",
                (run_id, spec.site_id, Jsonb(site_modules.check_state(spec.module.state))),
            )
        await wallet.record_event(
            conn,
            run_id,
            "web_site_created",
            run["current_window"],
            {
                "site_id": spec.site_id,
                "name": spec.name,
                "kind": spec.kind,
                "tagline": spec.tagline,
                "source": source,
            },
            f"web:site:{spec.site_id}",
        )
        return _summary(site)


async def _check_module(conn, run_id, spec: SiteSpec, source):
    """A module site comes only from the frozen plot, and its module and people must exist."""
    if spec.module is None:
        return
    if source != SEEDED_SOURCE:
        raise SiteError("Only a site from the plot can use a module.", 422)
    known = await _one(
        conn,
        "SELECT 1 FROM web_modules WHERE run_id=%s AND module_id=%s",
        (run_id, spec.module.id),
    )
    if known is None:
        raise SiteError("The site names a module that this run does not hold.", 422)
    state = site_modules.check_state(spec.module.state)
    wanted = set(state["accounts"]) | {
        name for record in state["records"].values() for name in record["access"]
    }
    found = await _rows(
        conn,
        "SELECT owner_id FROM run_participants WHERE run_id=%s AND owner_id=ANY(%s)",
        (run_id, list(wanted)),
    )
    if {entry["owner_id"] for entry in found} != wanted:
        raise SiteError("Site state names an unknown person.", 422)


async def seed_modules(conn, run_id, modules) -> None:
    """Save the built modules of a frozen plot. The source is checked again before it is saved."""
    for entry in modules:
        entry = entry if isinstance(entry, dict) else entry.model_dump(mode="json")
        try:
            loaded = site_modules.load(entry["source"])
        except site_modules.ModuleProblem as error:
            raise SiteError(f"Module {entry['id']} is not valid: {error}", 422) from None
        if loaded.sha256 != entry["sha256"]:
            raise SiteError(f"Module {entry['id']} does not match its hash.", 422)
        await conn.execute(
            """INSERT INTO web_modules(run_id,module_id,sha256,source) VALUES (%s,%s,%s,%s)
               ON CONFLICT (run_id,module_id) DO NOTHING""",
            (run_id, entry["id"], entry["sha256"], entry["source"]),
        )


async def seed_sites(conn, run_id, specs) -> list[dict]:
    """Save the sites named by a frozen plot. They are not subject to max_sites."""
    return [
        await create_site(conn, run_id, spec, source=SEEDED_SOURCE, enforce_limit=False)
        for spec in specs
    ]


_MERGED_LISTS = ("items", "postings", "slots", "bills", "pages")
_RECORD_STATUS = {
    "listings": ("web_listings", "listing_id", {"active", "removed", "sold"}),
    "applications": ("web_applications", "application_id", {"submitted", "accepted", "declined"}),
}


def _merge_patch(spec: dict, patch: dict) -> dict:
    merged = copy.deepcopy(spec)
    for key, value in patch.items():
        if key in _RECORD_STATUS:
            continue
        if key == "module" or (spec.get("module") and key in _MERGED_LISTS + ("policies",)):
            raise SiteError("A site update cannot change a module or add pages beside it.", 422)
        if key in {"site_id", "kind"}:
            if value != spec[key]:
                raise SiteError("A site update cannot change its address or kind.", 422)
        elif key in _MERGED_LISTS:
            if not isinstance(value, list) or not all(
                isinstance(change, dict) and isinstance(change.get("id"), str) for change in value
            ):
                raise SiteError("A site update needs list entries with IDs.", 422)
            current = {entry["id"]: entry for entry in merged[key]}
            for change in value:
                values = {name: item for name, item in change.items() if name != "remove"}
                if change.get("remove") is True:
                    current.pop(change["id"], None)
                else:
                    current[change["id"]] = {**current.get(change["id"], {}), **values}
            merged[key] = list(current.values())
        elif key == "fees":
            if not isinstance(value, dict):
                raise SiteError("A fee update must be an object.", 422)
            merged["fees"] = {**merged.get("fees", {}), **value}
        else:
            merged[key] = value
    return merged


async def update_site(conn, run_id, site_id, patch, *, operation_key, source="director") -> dict:
    """Apply one bounded patch and save a new site version.

    Top-level fields replace. items, postings, slots, bills and pages merge by id, and an
    entry with "remove": true leaves. A stock or capacity named in the patch resets the live
    count. listings and applications take [{"id", "status"}] and change saved records only,
    so a later review can mark a listing sold. The same key and patch return the same site.
    """
    if not isinstance(patch, dict) or len(json.dumps(patch, default=str)) > 64_000:
        raise SiteError("A site update must be a bounded object.", 422)
    patch_hash = _hash(patch)
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        site = await _one(
            conn, "SELECT * FROM web_sites WHERE run_id=%s AND site_id=%s", (run_id, site_id)
        )
        if site is None:
            raise SiteError("Site not found.", 404)
        prior = await _one(
            conn,
            "SELECT site_id,patch_hash FROM web_site_versions WHERE run_id=%s AND operation_key=%s",
            (run_id, operation_key),
        )
        if prior is not None:
            if prior["site_id"] != site_id or prior["patch_hash"] != patch_hash:
                raise SiteError("The operation key was used with a different update.")
            return _summary(site)
        spec = _validated(_merge_patch(site["spec"], patch))
        await _check_bill_owners(conn, run_id, spec)
        document = spec.model_dump(mode="json")
        sim_s = await wallet.sim_now(conn, run_id)
        updated = await _one(
            conn,
            """UPDATE web_sites SET version=version+1,name=%s,spec=%s,
               search_doc=setweight(to_tsvector('english',%s),'A')
                          ||setweight(to_tsvector('english',%s),'B')
               WHERE run_id=%s AND site_id=%s RETURNING *""",
            (
                spec.name,
                Jsonb(document),
                f"{spec.name} {spec.tagline}",
                _site_text(spec),
                run_id,
                site_id,
            ),
        )
        await conn.execute(
            """INSERT INTO web_site_versions(run_id,site_id,version,spec,spec_hash,patch,
                      patch_hash,operation_key,source,sim_s)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                run_id,
                site_id,
                updated["version"],
                Jsonb(document),
                _hash(document),
                Jsonb(patch),
                patch_hash,
                operation_key,
                source,
                sim_s,
            ),
        )
        await _apply_entry_changes(conn, run_id, site_id, spec, patch, sim_s)
        await wallet.record_event(
            conn,
            run_id,
            "web_site_updated",
            run["current_window"],
            {"site_id": site_id, "version": updated["version"], "changed": sorted(patch)},
            f"web:site-update:{operation_key}",
        )
        return _summary(updated)


async def _apply_entry_changes(conn, run_id, site_id, spec: SiteSpec, patch, sim_s):
    items = {entry.id: entry for entry in spec.items}
    slots = {entry.id: entry for entry in spec.slots}
    for change in patch.get("items", ()):
        if change["id"] not in items:
            await conn.execute(
                """DELETE FROM web_inventory WHERE run_id=%s AND site_id=%s
                   AND entry_kind='item' AND entry_id=%s""",
                (run_id, site_id, change["id"]),
            )
            continue
        known = await _one(
            conn,
            """SELECT remaining FROM web_inventory WHERE run_id=%s AND site_id=%s
               AND entry_kind='item' AND entry_id=%s""",
            (run_id, site_id, change["id"]),
        )
        if "stock" in change or known is None:
            await _save_inventory(
                conn, run_id, site_id, "item", change["id"], items[change["id"]].stock
            )
    for change in patch.get("slots", ()):
        if change["id"] not in slots:
            await conn.execute(
                """DELETE FROM web_inventory WHERE run_id=%s AND site_id=%s
                   AND entry_kind='slot' AND entry_id=%s""",
                (run_id, site_id, change["id"]),
            )
            continue
        known = await _one(
            conn,
            """SELECT count(*) AS n FROM web_bookings WHERE run_id=%s AND site_id=%s
               AND slot_id=%s AND status='booked'""",
            (run_id, site_id, change["id"]),
        )
        remaining = max(0, slots[change["id"]].capacity - known["n"])
        await _save_inventory(conn, run_id, site_id, "slot", change["id"], remaining)
    for change in patch.get("bills", ()):
        if change.get("remove") is True:
            await conn.execute(
                "DELETE FROM web_bills WHERE run_id=%s AND site_id=%s AND bill_id=%s "
                "AND status='unpaid'",
                (run_id, site_id, change["id"]),
            )
    for bill in spec.bills:
        await _save_bill(conn, run_id, site_id, bill, sim_s)
    for name, (table, column, allowed) in _RECORD_STATUS.items():
        for change in patch.get(name, ()):
            if (
                not isinstance(change, dict)
                or not isinstance(change.get("id"), str)
                or change.get("status") not in allowed
            ):
                raise SiteError("A saved record update needs an ID and a known status.", 422)
            # table and column are constants from _RECORD_STATUS, not input.
            version = ",version=version+1" if table == "web_listings" else ""
            await conn.execute(
                f"UPDATE {table} SET status=%s{version} "
                f"WHERE run_id=%s AND site_id=%s AND {column}=%s",
                (change["status"], run_id, site_id, change["id"]),
            )


async def bill_status(conn, run_id, site_id, bill_id) -> dict | None:
    """Return one saved bill, or None. A lifecycle check reads this."""
    return await _one(
        conn,
        "SELECT * FROM web_bills WHERE run_id=%s AND site_id=%s AND bill_id=%s",
        (run_id, site_id, bill_id),
    )


async def search_sites(conn, run_id, query, *, limit=SEARCH_LIMIT) -> list[dict]:
    """Rank this run's sites with PostgreSQL full-text search.

    Common shopping and address words count only when the query has nothing else, so "pawn shop
    near me" does not match every shop, and a number never decides a match. A site fits when its
    own name is searched for, or when it matches at least half of the distinctive words, and two
    of them if there are two or more, so "free bet" does not find every site that says free.
    When nothing fits, a long natural query still finds the one best site from the plot that
    matches two or more distinctive words. A site built on the spot gets no such reach, so a new
    need can still get its own site. Order is deterministic: name, match count, rank, site ID.
    """
    found = await _rows(
        conn,
        """WITH base AS (
             SELECT tsvector_to_array(to_tsvector('english',%(q)s)) AS lexemes,
                    tsvector_to_array(to_tsvector('english',%(generic)s)) AS generic,
                    replace(plainto_tsquery('english',%(q)s)::text,' & ',' | ') AS either
           ), wanted AS (
             SELECT COALESCE(
                      NULLIF(ARRAY(SELECT word FROM unnest(lexemes) AS word WHERE word !~ '^[0-9]'
                                   EXCEPT SELECT unnest(generic)),'{}'),
                      lexemes) AS specific,
                    NULLIF(either,'')::tsquery AS either
             FROM base
           ), scored AS (
             SELECT s.site_id,s.name,s.kind,s.source,s.spec->>'tagline' AS tagline,
                    cardinality(ARRAY(SELECT unnest(tsvector_to_array(s.search_doc))
                                      INTERSECT SELECT unnest(w.specific))) AS matched,
                    cardinality(w.specific) AS wanted_count,
                    ts_rank_cd(s.search_doc,w.either) AS rank,
                    tsvector_to_array(to_tsvector('english',split_part(s.name,' ',1)))
                      && w.specific AS named
             FROM web_sites s,wanted w
             WHERE s.run_id=%(run)s AND s.search_doc @@ w.either
           ), judged AS (
             SELECT *,matched>0 AND (named OR (matched*2>=wanted_count
                      AND (matched>=2 OR wanted_count<=1))) AS fits
             FROM scored
           )
           SELECT site_id,name,kind,tagline,fits FROM judged
           WHERE fits OR (matched>=2 AND source=%(seeded)s)
           ORDER BY named DESC,matched DESC,rank DESC,site_id LIMIT %(limit)s""",
        {
            "q": query,
            "generic": _GENERIC_SEARCH_WORDS,
            "run": run_id,
            "limit": limit,
            "seeded": SEEDED_SOURCE,
        },
    )
    keep = [site for site in found if site["fits"]] or found[:1]
    return [{key: site[key] for key in ("site_id", "name", "kind", "tagline")} for site in keep]


def query_key(query: str) -> str:
    return hashlib.sha256(" ".join(query.lower().split()).encode()).hexdigest()[:32]


async def reserve_search_ask(conn, run_id, owner_id, query) -> str | None:
    """Claim one model request for a search with no result, or return None.

    The same normalized search asks once. A claim left by an interrupted call can ask again,
    up to MAX_SEARCH_ASK_ATTEMPTS. A run stops asking at max_sites sites, or after three
    asks per permitted site, so repeated empty searches cannot grow model cost without limit.
    """
    run = await _WORLD._run(conn, run_id)
    terms = await _one(conn, "SELECT numnode(plainto_tsquery('english',%s)) AS n", (query,))
    if not terms["n"]:
        return None  # Only common stop words. There is nothing to build a site for.
    key = query_key(query)
    counts = await _one(
        conn,
        """SELECT (SELECT count(*) FROM web_sites WHERE run_id=%s) AS sites,
           (SELECT count(*) FROM web_search_asks WHERE run_id=%s) AS asks""",
        (run_id, run_id),
    )
    limit = max_sites(run)
    if counts["sites"] >= limit:
        return None
    existing = await _one(
        conn,
        "SELECT status,attempts FROM web_search_asks WHERE run_id=%s AND query_key=%s",
        (run_id, key),
    )
    if existing is not None:
        if existing["status"] != "asked" or existing["attempts"] >= MAX_SEARCH_ASK_ATTEMPTS:
            return None
        await conn.execute(
            "UPDATE web_search_asks SET attempts=attempts+1 WHERE run_id=%s AND query_key=%s",
            (run_id, key),
        )
        return key
    if counts["asks"] >= limit * 3:
        return None
    await conn.execute(
        """INSERT INTO web_search_asks(run_id,query_key,owner_id,query,sim_s)
           VALUES (%s,%s,%s,%s,%s)""",
        (run_id, key, owner_id, query[:200], await wallet.sim_now(conn, run_id)),
    )
    return key


async def release_search_ask(conn, run_id, key):
    """No model answered. An outage is not an attempt, and the same call runs again.

    The claim itself counts as attempt 1, so the count never goes below that.
    """
    await conn.execute(
        "UPDATE web_search_asks SET attempts=GREATEST(attempts-1,1) "
        "WHERE run_id=%s AND query_key=%s AND status='asked'",
        (run_id, key),
    )


async def finish_search_ask(conn, run_id, key, site_id=None):
    await conn.execute(
        "UPDATE web_search_asks SET status=%s,site_id=%s WHERE run_id=%s AND query_key=%s",
        ("created" if site_id else "none", site_id, run_id, key),
    )


# ---------------------------------------------------------------------------
# Page building
# ---------------------------------------------------------------------------


def _money(amount) -> str:
    return f"${amount:,}"


def _short(text: str, limit: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class _View:
    conn: object
    run: dict
    site: dict
    spec: dict
    owner_id: str | None
    now_s: int

    @property
    def run_id(self):
        return self.run["id"]

    @property
    def site_id(self) -> str:
        return self.site["site_id"]

    def url(self, path: str = "/", **query) -> str:
        suffix = f"?{urlencode(query)}" if query else ""
        return site_url(self.site_id, path) + suffix

    def when(self, seconds: int) -> str | None:
        start = wallet.clock_section(self.run).get("start_at")
        if not start:
            return None
        try:
            return clock.format_time(clock.parse_start(start) + timedelta(seconds=seconds))
        except clock.ClockError:
            return None

    def has(self, name: str) -> bool:
        return bool(self.spec.get(name))

    @property
    def kind(self) -> str:
        return self.spec["kind"]


def _available_actions(spec: dict) -> set[str]:
    actions = {"contact"}
    if spec.get("items"):
        actions |= {
            "search",
            "add_to_cart",
            "remove_from_cart",
            "view_cart",
            "checkout",
            "order_status",
            "cancel_order",
        }
    if spec["kind"] == "marketplace":
        actions |= {
            "search",
            "create_listing",
            "update_listing",
            "remove_listing",
            "view_listings",
        }
    if spec.get("postings"):
        actions |= {"search", "apply"}
    if spec.get("slots"):
        actions |= {"search", "book", "cancel_booking"}
    if spec.get("bills") or spec["kind"] == "portal":
        actions |= {"view_bills", "pay_bill"}
    return actions


def _nav(view: _View) -> list[Link]:
    links = [Link("Home", view.url())]
    if view.has("items"):
        links += [
            Link("Catalog", view.url("/items")),
            Link("Cart", view.url("/cart")),
            Link("Orders", view.url("/orders")),
        ]
    if view.kind == "marketplace":
        links.append(Link("Listings", view.url("/listings")))
    if view.has("postings"):
        links.append(Link("Open jobs", view.url("/postings")))
    if view.has("slots"):
        links += [
            Link("Times", view.url("/slots")),
            Link("Your bookings", view.url("/bookings")),
        ]
    if view.has("bills") or view.kind == "portal":
        links.append(Link("Bills", view.url("/bills")))
    links.append(Link("Contact", view.url("/contact")))
    if view.has("policies"):
        links.append(Link("Policies", view.url("/policies")))
    links.extend(
        Link(page["title"], view.url(f"/pages/{page['id']}")) for page in view.spec["pages"]
    )
    return links


async def _inventory(view: _View, kind: str) -> dict[str, int]:
    found = await _rows(
        view.conn,
        """SELECT entry_id,remaining FROM web_inventory
           WHERE run_id=%s AND site_id=%s AND entry_kind=%s""",
        (view.run_id, view.site_id, kind),
    )
    return {entry["entry_id"]: entry["remaining"] for entry in found}


def _stock_text(remaining: int) -> str:
    return f"{remaining} in stock" if remaining else "Sold out"


def _item_line(view: _View, item: dict, stock: dict) -> str:
    return (
        f"- {item['title']} | {_money(item['price'])} | "
        f"{_stock_text(stock.get(item['id'], 0))} | item_id: {item['id']} | "
        f"{view.url('/items/' + item['id'])}"
    )


def _add_form(view: _View, path: str, item_id: str | None = None) -> Form:
    return Form(
        "Add to cart",
        view.url(path),
        "add_to_cart",
        (
            FormField("item_id", required=item_id is None, value=item_id),
            FormField("quantity", hint="default 1"),
        ),
    )


def _matches(query: str, *values) -> int:
    words = {word for word in re.findall(r"\w+", query.lower()) if len(word) > 1}
    text = " ".join(str(value or "") for value in values).lower()
    return sum(word in text for word in words)


def _page_number(query: dict) -> int:
    value = str(query.get("page", "1"))
    return max(1, min(int(value), 1000)) if value.isascii() and value.isdigit() else 1


async def _items_page(view: _View, query: dict) -> Page:
    stock = await _inventory(view, "item")
    items = view.spec["items"]
    search = _short(query.get("q", ""), 200)
    if search:
        scored = [
            (_matches(search, i["title"], i["category"], i["description"]), index, i)
            for index, i in enumerate(items)
        ]
        items = [entry[2] for entry in sorted(scored, key=lambda v: (-v[0], v[1])) if entry[0]]
    number = _page_number(query)
    start = (number - 1) * PAGE_SIZE
    shown = items[start : start + PAGE_SIZE]
    section = Section(
        heading=f'Results for "{search}"' if search else "Catalog",
        lines=[_item_line(view, item, stock) for item in shown] or ["Nothing found."],
    )
    if start + PAGE_SIZE < len(items):
        args = {"page": number + 1, **({"q": search} if search else {})}
        section.links.append(Link("More", view.url("/items", **args)))
    section.forms.append(_add_form(view, "/items"))
    section.forms.append(
        Form("Search this site", view.url("/items"), "search", (FormField("q", required=True),))
    )
    return _page(view, "/items", "Catalog", [section])


async def _item_page(view: _View, item_id: str) -> Page | None:
    item = next((entry for entry in view.spec["items"] if entry["id"] == item_id), None)
    if item is None:
        return None
    stock = await _inventory(view, "item")
    lines = [f"Price: {_money(item['price'])}", _stock_text(stock.get(item_id, 0))]
    if item["category"]:
        lines.append(f"Category: {item['category']}")
    if item["description"]:
        lines.append(item["description"])
    section = Section(lines=lines, forms=[_add_form(view, f"/items/{item_id}", item_id)])
    return _page(view, f"/items/{item_id}", item["title"], [section])


def _account_needed(view: _View, path: str, title: str) -> Page:
    return _page(view, path, title, [Section(lines=["Choose a person above to see this page."])])


async def _cart_lines(view: _View) -> list[dict]:
    return await _rows(
        view.conn,
        """SELECT item_id,quantity FROM web_carts WHERE run_id=%s AND owner_id=%s AND site_id=%s
           ORDER BY item_id""",
        (view.run_id, view.owner_id, view.site_id),
    )


async def _cart_page(view: _View, notice: str = "") -> Page:
    if view.owner_id is None:
        return _account_needed(view, "/cart", "Cart")
    items = {entry["id"]: entry for entry in view.spec["items"]}
    lines, subtotal = [], 0
    for line in await _cart_lines(view):
        item = items.get(line["item_id"])
        if item is None:
            lines.append(f"- {line['item_id']} is no longer available.")
            continue
        cost = item["price"] * line["quantity"]
        subtotal += cost
        lines.append(
            f"- {line['quantity']} x {item['title']} at {_money(item['price'])} each: "
            f"{_money(cost)} | item_id: {item['id']}"
        )
    section = Section(heading="Your cart", lines=lines or ["Your cart is empty."])
    if lines:
        fee = view.spec["fees"]["delivery"]
        section.lines += [
            f"Subtotal: {_money(subtotal)}",
            f"Delivery: {_money(fee)}",
            f"Total: {_money(subtotal + fee)}",
            "Payment: your saved card.",
        ]
        section.forms += [
            Form(
                "Check out",
                view.url("/cart"),
                "checkout",
                (FormField("note", hint="delivery note"),),
            ),
            Form(
                "Remove from cart",
                view.url("/cart"),
                "remove_from_cart",
                (FormField("item_id", required=True), FormField("quantity", hint="default all")),
            ),
        ]
    return _page(view, "/cart", "Cart", [section], notice)


def _order_state(view: _View, order: dict) -> str:
    if order["status"] == "cancelled":
        return "Cancelled"
    if view.now_s >= order["eta_s"]:
        return "Delivered"
    moment = view.when(order["eta_s"])
    return f"On its way, expected {moment}" if moment else "On its way"


async def _orders_page(view: _View, notice: str = "") -> Page:
    if view.owner_id is None:
        return _account_needed(view, "/orders", "Orders")
    orders = await _rows(
        view.conn,
        """SELECT * FROM web_orders WHERE run_id=%s AND owner_id=%s AND site_id=%s
           ORDER BY sim_s DESC,order_id LIMIT %s""",
        (view.run_id, view.owner_id, view.site_id, PAGE_SIZE),
    )
    lines = [
        f"- {order['order_id']} | {_money(order['total'])} | {_order_state(view, order)} | "
        f"{view.url('/orders/' + order['order_id'])}"
        for order in orders
    ]
    section = Section(heading="Your orders", lines=lines or ["You have no orders here."])
    return _page(view, "/orders", "Orders", [section], notice)


async def _order_page(view: _View, order_id: str, notice: str = "") -> Page | None:
    if view.owner_id is None:
        return _account_needed(view, f"/orders/{order_id}", "Order")
    order = await _one(
        view.conn,
        "SELECT * FROM web_orders WHERE run_id=%s AND owner_id=%s AND site_id=%s AND order_id=%s",
        (view.run_id, view.owner_id, view.site_id, order_id),
    )
    if order is None:
        return None
    lines = [f"Status: {_order_state(view, order)}"]
    lines += [
        f"- {line['quantity']} x {line['title']} at {_money(line['unit_price'])} each"
        for line in order["lines"]
    ]
    lines += [f"Delivery: {_money(order['delivery_fee'])}", f"Total: {_money(order['total'])}"]
    section = Section(lines=lines)
    if order["status"] == "placed" and view.now_s < order["eta_s"]:
        section.forms.append(
            Form(
                "Cancel order",
                view.url(f"/orders/{order_id}"),
                "cancel_order",
                (FormField("order_id", value=order_id),),
            )
        )
    return _page(view, f"/orders/{order_id}", f"Order {order_id}", [section], notice)


def _listing_form(view: _View) -> Form:
    return Form(
        "Post a listing",
        view.url("/listings"),
        "create_listing",
        (
            FormField("title", required=True),
            FormField("price", required=True, hint="whole dollars"),
            FormField("description"),
            FormField("category"),
        ),
    )


def _listing_line(view: _View, listing: dict, *, own: bool) -> str:
    state = "" if listing["status"] == "active" else f" | {listing['status']}"
    who = "yours" if own else "private seller"
    return (
        f"- {listing['title']} | {_money(listing['price'])} | {who}{state} | "
        f"{view.url('/listings/' + listing['listing_id'])}"
    )


async def _listings_page(view: _View, query: dict, notice: str = "") -> Page:
    public = await _rows(
        view.conn,
        """SELECT * FROM web_listings WHERE run_id=%s AND site_id=%s AND status='active'
           AND owner_id IS DISTINCT FROM %s ORDER BY sim_s DESC,listing_id LIMIT %s""",
        (view.run_id, view.site_id, view.owner_id, PAGE_SIZE),
    )
    search = _short(query.get("q", ""), 200)
    if search:
        public = [
            entry
            for entry in public
            if _matches(search, entry["title"], entry["category"], entry["description"])
        ]
    sections = []
    if view.owner_id is not None:
        mine = await _rows(
            view.conn,
            """SELECT * FROM web_listings WHERE run_id=%s AND site_id=%s AND owner_id=%s
               AND status<>'removed' ORDER BY sim_s DESC,listing_id LIMIT %s""",
            (view.run_id, view.site_id, view.owner_id, PAGE_SIZE),
        )
        sections.append(
            Section(
                heading="Your listings",
                lines=[_listing_line(view, entry, own=True) for entry in mine]
                or ["You have no listings."],
                forms=[_listing_form(view)],
            )
        )
    sections.append(
        Section(
            heading=f'Results for "{search}"' if search else "For sale",
            lines=[_listing_line(view, entry, own=False) for entry in public]
            or ["Nothing listed right now."],
        )
    )
    return _page(view, "/listings", "Listings", sections, notice)


async def _listing_page(view: _View, listing_id: str, notice: str = "") -> Page | None:
    listing = await _one(
        view.conn,
        "SELECT * FROM web_listings WHERE run_id=%s AND site_id=%s AND listing_id=%s",
        (view.run_id, view.site_id, listing_id),
    )
    if listing is None:
        return None
    own = listing["owner_id"] == view.owner_id
    if listing["status"] != "active" and not own:
        return None
    lines = [f"Price: {_money(listing['price'])}", f"Status: {listing['status']}"]
    if listing["category"]:
        lines.append(f"Category: {listing['category']}")
    if listing["description"]:
        lines.append(listing["description"])
    path = f"/listings/{listing_id}"
    if own and listing["status"] == "active":
        forms = [
            Form(
                "Change listing",
                view.url(path),
                "update_listing",
                (
                    FormField("listing_id", value=listing_id),
                    FormField("title"),
                    FormField("price"),
                    FormField("description"),
                ),
            ),
            Form(
                "Remove listing",
                view.url(path),
                "remove_listing",
                (FormField("listing_id", value=listing_id),),
            ),
        ]
    elif own:
        forms = []
    else:
        forms = [
            Form(
                "Message the seller",
                view.url(path),
                "contact",
                (FormField("listing_id", value=listing_id), FormField("message", required=True)),
            )
        ]
    return _page(view, path, listing["title"], [Section(lines=lines, forms=forms)], notice)


async def _postings_page(view: _View) -> Page:
    lines = [
        "- "
        + " | ".join(
            part
            for part in (
                posting["title"],
                posting["pay"],
                posting["location"],
                view.url("/postings/" + posting["id"]),
            )
            if part
        )
        for posting in view.spec["postings"][:PAGE_SIZE]
    ]
    return _page(
        view,
        "/postings",
        "Open jobs",
        [Section(heading="Open jobs", lines=lines or ["No open jobs right now."])],
    )


async def _posting_page(view: _View, posting_id: str, notice: str = "") -> Page | None:
    posting = next((p for p in view.spec["postings"] if p["id"] == posting_id), None)
    if posting is None:
        return None
    lines = [
        value
        for value in (
            f"Pay: {posting['pay']}" if posting["pay"] else "",
            f"Location: {posting['location']}" if posting["location"] else "",
            posting["description"],
        )
        if value
    ]
    if view.owner_id is not None:
        prior = await _one(
            view.conn,
            """SELECT status FROM web_applications WHERE run_id=%s AND owner_id=%s AND site_id=%s
               AND posting_id=%s""",
            (view.run_id, view.owner_id, view.site_id, posting_id),
        )
        if prior is not None:
            lines.append(f"Your application: {prior['status']}")
    path = f"/postings/{posting_id}"
    form = Form(
        "Apply",
        view.url(path),
        "apply",
        (FormField("posting_id", value=posting_id), FormField("message", required=True)),
    )
    return _page(view, path, posting["title"], [Section(lines=lines, forms=[form])], notice)


async def _slots_page(view: _View, notice: str = "") -> Page:
    places = await _inventory(view, "slot")
    lines = []
    for slot in view.spec["slots"][:PAGE_SIZE]:
        left = places.get(slot["id"], 0)
        parts = [slot["title"], slot["when"], _money(slot["price"]) if slot["price"] else "Free"]
        parts += [f"{left} open" if left else "Full", f"slot_id: {slot['id']}"]
        lines.append("- " + " | ".join(part for part in parts if part))
    form = Form(
        "Book",
        view.url("/slots"),
        "book",
        (FormField("slot_id", required=True), FormField("note")),
    )
    section = Section(heading="Times", lines=lines or ["No times available."], forms=[form])
    return _page(view, "/slots", "Times", [section], notice)


async def _bookings_page(view: _View, notice: str = "") -> Page:
    if view.owner_id is None:
        return _account_needed(view, "/bookings", "Your bookings")
    bookings = await _rows(
        view.conn,
        """SELECT * FROM web_bookings WHERE run_id=%s AND owner_id=%s AND site_id=%s
           ORDER BY sim_s DESC,booking_id LIMIT %s""",
        (view.run_id, view.owner_id, view.site_id, PAGE_SIZE),
    )
    lines = [
        f"- {entry['title']} | {_money(entry['price'])} | {entry['status']} | "
        f"booking_id: {entry['booking_id']}"
        for entry in bookings
    ]
    section = Section(heading="Your bookings", lines=lines or ["You have no bookings here."])
    if any(entry["status"] == "booked" for entry in bookings):
        section.forms.append(
            Form(
                "Cancel booking",
                view.url("/bookings"),
                "cancel_booking",
                (FormField("booking_id", required=True),),
            )
        )
    return _page(view, "/bookings", "Your bookings", [section], notice)


async def _bills_page(view: _View, notice: str = "") -> Page:
    if view.owner_id is None:
        return _account_needed(view, "/bills", "Bills")
    bills = await _rows(
        view.conn,
        """SELECT * FROM web_bills WHERE run_id=%s AND site_id=%s AND owner_id=%s
           ORDER BY status DESC,bill_id""",
        (view.run_id, view.site_id, view.owner_id),
    )
    lines = []
    for bill in bills:
        parts = [bill["title"], _money(bill["amount"]), bill["status"]]
        if bill["due_text"]:
            parts.insert(2, f"due {bill['due_text']}")
        parts.append(f"bill_id: {bill['bill_id']}")
        lines.append("- " + " | ".join(parts))
    section = Section(heading="Your bills", lines=lines or ["You have no bills here."])
    if any(bill["status"] == "unpaid" for bill in bills):
        section.forms.append(
            Form(
                "Pay a bill",
                view.url("/bills"),
                "pay_bill",
                (FormField("bill_id", required=True),),
            )
        )
    return _page(view, "/bills", "Bills", [section], notice)


async def _contact_page(view: _View, notice: str = "") -> Page:
    form = Form(
        "Send a message",
        view.url("/contact"),
        "contact",
        (FormField("subject"), FormField("message", required=True)),
    )
    return _page(view, "/contact", "Contact", [Section(forms=[form])], notice)


async def _home_page(view: _View) -> Page:
    about = Section(
        lines=[value for value in (view.spec["tagline"], view.spec["description"]) if value]
    )
    sections = [about] if about.lines else []
    if view.has("items"):
        stock = await _inventory(view, "item")
        sections.append(
            Section(
                heading="Popular",
                lines=[_item_line(view, item, stock) for item in view.spec["items"][:8]],
                forms=[
                    Form(
                        "Search this site",
                        view.url("/items"),
                        "search",
                        (FormField("q", required=True),),
                    )
                ],
            )
        )
    if view.kind == "marketplace":
        sections.append(
            Section(
                heading="Buy and sell",
                links=[Link("See listings", view.url("/listings"))],
                forms=[_listing_form(view)],
            )
        )
    if view.has("postings"):
        sections.append((await _postings_page(view)).sections[0])
    if view.has("slots"):
        sections.append((await _slots_page(view)).sections[0])
    if view.has("bills") or view.kind == "portal":
        sections.append(
            Section(heading="Your account", links=[Link("See your bills", view.url("/bills"))])
        )
    return _page(view, "/", "Home", sections)


def _policies_page(view: _View) -> Page:
    lines = [f"- {policy}" for policy in view.spec["policies"]] or ["No policies listed."]
    return _page(view, "/policies", "Policies", [Section(lines=lines)])


def _page(view: _View, path: str, title: str, sections: list, notice: str = "") -> Page:
    return Page(
        url=view.url(path),
        title=title,
        site_name=view.site["name"],
        notice=notice,
        sections=sections,
        nav=_nav(view),
    )


def _site_not_found(view: _View, path: str) -> Page:
    page = _page(
        view,
        path,
        "Page not found",
        [Section(lines=["We can't find that page. Try the links below."])],
    )
    page.status = 404
    return page


async def _route(view: _View, path: str, query: dict, notice: str = "") -> Page:
    parts = [part for part in path.split("/") if part]
    head = parts[0] if parts else ""
    entry = parts[1] if len(parts) == 2 else None
    page: Page | None = None
    if view.spec.get("module"):
        return await _module_route(view, path, query, notice)
    if len(parts) > 2:
        page = None
    elif not parts:
        page = await _home_page(view)
    elif entry is None and head == "items" and view.has("items"):
        page = await _items_page(view, query)
    elif entry is not None and head == "items":
        page = await _item_page(view, entry)
    elif entry is None and head == "cart" and view.has("items"):
        page = await _cart_page(view)
    elif entry is None and head == "orders" and view.has("items"):
        page = await _orders_page(view)
    elif entry is not None and head == "orders":
        page = await _order_page(view, entry)
    elif entry is None and head == "listings" and view.kind == "marketplace":
        page = await _listings_page(view, query)
    elif entry is not None and head == "listings":
        page = await _listing_page(view, entry)
    elif entry is None and head == "postings" and view.has("postings"):
        page = await _postings_page(view)
    elif entry is not None and head == "postings":
        page = await _posting_page(view, entry)
    elif entry is None and head == "slots" and view.has("slots"):
        page = await _slots_page(view)
    elif entry is None and head == "bookings" and view.has("slots"):
        page = await _bookings_page(view)
    elif entry is None and head == "bills" and (view.has("bills") or view.kind == "portal"):
        page = await _bills_page(view)
    elif entry is None and head == "contact":
        page = await _contact_page(view)
    elif entry is None and head == "policies" and view.has("policies"):
        page = _policies_page(view)
    elif entry is not None and head == "pages":
        custom = next((p for p in view.spec["pages"] if p["id"] == entry), None)
        if custom is not None:
            page = _page(view, path, custom["title"], [Section(lines=[custom["body"]])])
    page = page or _site_not_found(view, path)
    if notice:
        page.notice = notice
    return page


# ---------------------------------------------------------------------------
# Sites with a built module
# ---------------------------------------------------------------------------

MODULE_FAILED = "This page is not available right now. Please try again later."


def module_context(run: dict, owner_id, now_s: int) -> dict:
    """Story time and the visitor's name, as a module may read them."""
    context = {"minutes": now_s // 60, "viewer_name": ""}
    for owner in (run.get("scenario") or {}).get("owners", ()):
        if owner.get("id") == owner_id:
            context["viewer_name"] = owner.get("name", "")
    start = wallet.clock_section(run).get("start_at")
    if start:
        try:
            moment = clock.parse_start(start) + timedelta(seconds=now_s)
        except clock.ClockError:
            return context
        context.update(
            now=moment.strftime("%Y-%m-%dT%H:%M"),
            date=moment.strftime("%Y-%m-%d"),
            weekday=moment.strftime("%A"),
        )
    return context


def module_page(site_id, site_name, module, data, path, notice="", status=200) -> Page:
    """Turn a module's page description into the one Page that both renderers write."""
    sections = []
    for part in data["sections"]:
        forms = []
        for form in part["forms"]:
            declared = module.actions[form["action"]].fields
            forms.append(
                Form(
                    form["label"],
                    site_url(site_id, form["path"]),
                    form["action"],
                    tuple(
                        FormField(
                            name,
                            required=item.required,
                            value=form["values"].get(name),
                            hint=item.describe(),
                        )
                        for name, item in declared.items()
                    ),
                )
            )
        sections.append(
            Section(
                heading=part["heading"],
                lines=list(part["lines"]),
                links=[
                    Link(link["label"], site_url(site_id, link["path"])) for link in part["links"]
                ],
                forms=forms,
            )
        )
    nav = [Link("Home", site_url(site_id, "/"))]
    nav += [Link(label, site_url(site_id, target)) for label, target in module.nav]
    return Page(
        url=site_url(site_id, path),
        title=data["title"],
        site_name=site_name,
        status=status,
        notice=notice,
        sections=sections,
        nav=nav,
    )


def module_failed_page(site_id, site_name, path) -> Page:
    return Page(
        url=site_url(site_id, path),
        title="Service unavailable",
        site_name=site_name,
        status=500,
        sections=[Section(lines=[MODULE_FAILED])],
        nav=[Link("Home", site_url(site_id, "/"))],
    )


async def _module_parts(view: _View, *, lock=False):
    source = await _one(
        view.conn,
        "SELECT source FROM web_modules WHERE run_id=%s AND module_id=%s",
        (view.run_id, view.spec["module"]["id"]),
    )
    saved = await _one(
        view.conn,
        "SELECT state,version FROM web_module_state WHERE run_id=%s AND site_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (view.run_id, view.site_id),
    )
    if source is None or saved is None:
        raise site_modules.ModuleRunError("The site has no saved module.")
    try:
        return site_modules.load(source["source"]), saved["state"]
    except site_modules.ModuleProblem as error:
        raise site_modules.ModuleRunError(str(error)) from None


async def _module_route(view: _View, path: str, query: dict, notice: str = "") -> Page:
    """Build one page of a module site from saved state. It writes nothing."""
    name = view.site["name"]
    try:
        module, state = await _module_parts(view)
        context = module_context(view.run, view.owner_id, view.now_s)
        data = site_modules.render_page(module, state, view.owner_id, context, path, query)
    except site_modules.ModuleRunError:
        return module_failed_page(view.site_id, name, path)
    if data is None:
        return _site_not_found(view, path)
    return module_page(view.site_id, name, module, data, path, notice)


async def _submit_module_action(conn, run_id, owner_id, site_id, action, fields, request_id):
    """Run one module action in one transaction: state, money, messages and the saved result.

    The module computes a change from the visitor's view. Code here applies it. A declined
    card leaves the state as it was. A module fault saves a web_module_error event, changes
    nothing else and saves no result, so the same request can be sent again.
    """
    if not isinstance(request_id, str) or not _REQUEST_RE.fullmatch(request_id):
        raise SiteError("A valid request ID is required.", 422)
    if not isinstance(action, str) or not isinstance(fields, dict):
        raise SiteError("Please check the form fields.", 422)
    digest = _hash({"site_id": site_id, "action": action, "fields": fields})
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        prior = await _one(
            conn,
            """SELECT payload_hash,result FROM web_requests
               WHERE run_id=%s AND owner_id=%s AND request_id=%s""",
            (run_id, owner_id, request_id),
        )
        if prior is not None:
            if prior["payload_hash"] != digest:
                raise SiteError("This request ID was already used for a different form.")
            return prior["result"]
        _WORLD._active(run)
        await _WORLD._account(conn, run_id, owner_id)
        site = await _one(
            conn, "SELECT * FROM web_sites WHERE run_id=%s AND site_id=%s", (run_id, site_id)
        )
        if site is None or not site["spec"].get("module"):
            raise SiteError("Site not found.", 404)
        now_s = await wallet.sim_now(conn, run_id)
        view = _View(conn, run, site, site["spec"], owner_id, now_s)
        context = module_context(run, owner_id, now_s)
        try:
            module, state = await _module_parts(view, lock=True)
            if action not in module.actions:
                raise SiteError("This site does not offer that action.", 422)
            clean = site_modules.clean_fields(module.actions[action], fields)
            outcome = site_modules.run_action(module, state, owner_id, context, action, clean)
        except site_modules.FieldProblem as error:
            raise SiteError(str(error), 422) from None
        except site_modules.ModuleRunError as error:
            await _site_event(
                view,
                "web_module_error",
                f"web:module-error:{site_id}:{owner_id}:{request_id}",
                module_id=site["spec"]["module"]["id"],
                action=action,
                error=str(error)[:300],
            )
            return _result(module_failed_page(site_id, site["name"], "/"), "rejected")
        result_outcome, notice, charged = outcome.outcome, outcome.notice, 0
        if outcome.outcome == "done" and outcome.charge is not None:
            payment = await wallet.spend(
                conn,
                run_id,
                owner_id,
                outcome.charge["amount"],
                f"{site['name']}: {outcome.charge['memo']}"[:500],
                f"web:module:{site_id}:{owner_id}:{request_id}",
                f"web:{site_id}",
            )
            if payment["status"] == "declined":
                notice = await _decline(
                    view, action, outcome.charge["amount"], payment, request_id, action
                )
                result_outcome = "declined"
            else:
                charged = outcome.charge["amount"]
        applied = result_outcome == "done"
        if applied and outcome.changed:
            await conn.execute(
                """UPDATE web_module_state SET state=%s,version=version+1
                   WHERE run_id=%s AND site_id=%s""",
                (Jsonb(outcome.state), run_id, site_id),
            )
        if applied and outcome.message is not None:
            await _site_message(view, "contact", request_id, action=action, **outcome.message)
        if (
            applied
            and outcome.mail
            and await _one(conn, "SELECT 1 FROM clock_state WHERE run_id=%s", (run_id,))
        ):
            for index, letter in enumerate(outcome.mail):
                await clock.schedule(
                    conn,
                    run_id,
                    now_s + letter["in_minutes"] * 60,
                    "director_plan",
                    payload={
                        "mail": [
                            {
                                "to": owner_id,
                                "from_name": site["name"][:120],
                                "subject": letter["subject"],
                                "body": letter["body"],
                            }
                        ],
                        "about_owner": owner_id,
                    },
                    key=f"web-module-mail:{site_id}:{owner_id}:{request_id}:{index}",
                    owner_id=owner_id,
                    source="web",
                )
        if module.actions[action].write:
            await _site_event(
                view,
                "web_module_action",
                f"web:module-action:{site_id}:{owner_id}:{request_id}",
                module_id=site["spec"]["module"]["id"],
                action=action,
                outcome=result_outcome,
                charged=charged,
                request_id=request_id,
            )
        kept = applied or outcome.outcome == "rejected"
        target = outcome.path if kept else "/"
        shown = outcome.state if applied else state
        try:
            data = site_modules.render_page(
                module, shown, owner_id, context, target, outcome.query if kept else {}
            )
        except site_modules.ModuleRunError:
            data = None
        if data is None:
            data = {"title": site["name"], "sections": []}
        page = module_page(site_id, site["name"], module, data, target, notice)
        result = _result(page, result_outcome)
        payload = {"site_id": site_id, "action": action, "fields": fields}
        await conn.execute(
            """INSERT INTO web_requests(run_id,owner_id,request_id,payload_hash,payload,result,
                      sim_s) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, owner_id, request_id, digest, Jsonb(payload), Jsonb(result), now_s),
        )
        return result


async def _load_view(conn, run_id, site_id, owner_id) -> _View | None:
    run = await _one(conn, "SELECT id,scenario FROM environment_runs WHERE id=%s", (run_id,))
    if run is None:
        return None
    site = await _one(
        conn, "SELECT * FROM web_sites WHERE run_id=%s AND site_id=%s", (run_id, site_id)
    )
    if site is None:
        return None
    return _View(conn, run, site, site["spec"], owner_id, await wallet.sim_now(conn, run_id))


async def build_page(conn, run_id, site_id, path="/", query=None, owner_id=None) -> Page | None:
    """Build one page from saved rows. It writes nothing. None means no such site."""
    view = await _load_view(conn, run_id, site_id, owner_id)
    if view is None:
        return None
    return await _route(view, path, query or {})


async def open_url(conn, run_id, owner_id, url) -> Page:
    """Resolve an address to a page. An unknown host gets the plain not-found page."""
    site_id, path, query = parse_url(url)
    page = None
    if site_id is not None:
        page = await build_page(conn, run_id, site_id, path, query, owner_id)
    return page if page is not None else not_found_page(url)


# ---------------------------------------------------------------------------
# Deterministic actions
# ---------------------------------------------------------------------------


class _Form(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def valid_text(cls, value):
        return _clean(value)


_Quantity = Annotated[int, Field(ge=1, le=99)]
_Price = Annotated[int, Field(ge=0, le=1_000_000)]


class _SearchForm(_Form):
    q: _Query


class _AddForm(_Form):
    item_id: EntryID
    quantity: _Quantity = 1


class _RemoveForm(_Form):
    item_id: EntryID
    quantity: _Quantity | None = None


class _NoFields(_Form):
    pass


class _CheckoutForm(_Form):
    note: _Note | None = None


class _OrderForm(_Form):
    order_id: EntryID


class _OrderStatusForm(_Form):
    order_id: EntryID | None = None


class _ListingForm(_Form):
    title: _Title
    price: _Price
    description: OptionalText = ""
    category: _ShortLabel = ""


class _ChangeListingForm(_Form):
    listing_id: EntryID
    title: _Title | None = None
    price: _Price | None = None
    description: OptionalText | None = None


class _ListingRefForm(_Form):
    listing_id: EntryID


class _ApplyForm(_Form):
    posting_id: EntryID
    message: _Message


class _BillForm(_Form):
    bill_id: EntryID


class _BookForm(_Form):
    slot_id: EntryID
    note: _Note | None = None


class _BookingRefForm(_Form):
    booking_id: EntryID


class _ContactForm(_Form):
    message: _Message
    subject: _LongLine | None = None
    listing_id: EntryID | None = None
    posting_id: EntryID | None = None


READ_ACTIONS = {
    "search": _SearchForm,
    "view_cart": _NoFields,
    "order_status": _OrderStatusForm,
    "view_listings": _NoFields,
    "view_bills": _NoFields,
}
WRITE_ACTIONS = {
    "add_to_cart": _AddForm,
    "remove_from_cart": _RemoveForm,
    "checkout": _CheckoutForm,
    "cancel_order": _OrderForm,
    "create_listing": _ListingForm,
    "update_listing": _ChangeListingForm,
    "remove_listing": _ListingRefForm,
    "apply": _ApplyForm,
    "pay_bill": _BillForm,
    "book": _BookForm,
    "cancel_booking": _BookingRefForm,
    "contact": _ContactForm,
}
ACTIONS = tuple(READ_ACTIONS) + tuple(WRITE_ACTIONS)


def _result(page: Page, outcome: str = "done") -> dict:
    return {
        "outcome": outcome,
        "status": page.status,
        "url": page.url,
        "title": page.title,
        "text": render_text(page),
    }


def _validate_form(model, fields: dict) -> dict:
    try:
        return model.model_validate(fields).model_dump(exclude_none=True)
    except ValidationError as error:
        names = sorted(
            {
                re.sub(r"[^\w-]", "", str(item["loc"][0]))[:40]
                for item in error.errors()
                if item["loc"]
            }
        )
        detail = ", ".join(names) if names else "the form"
        raise SiteError(f"Please check these form fields: {detail}.", 422) from None


def _stable_id(prefix: str, run_id, owner_id: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}|{owner_id}|{request_id}".encode()).hexdigest()
    return f"{prefix}-{digest[:10].upper()}"


async def _decline(view: _View, action, amount, payment, request_id, target) -> str:
    """Save a declined card for later review and return the customer's message."""
    await wallet.record_event(
        view.conn,
        view.run_id,
        "web_spend_declined",
        view.run["current_window"],
        {
            "site_id": view.site_id,
            "site_name": view.site["name"],
            "action": action,
            "amount": amount,
            "reason": payment["reason"],
            "remaining": payment["remaining"],
            "target": target,
            "request_id": request_id,
        },
        f"web:declined:{view.owner_id}:{request_id}",
        view.owner_id,
    )
    return DECLINE_APPROVAL if payment["reason"] == "approval_required" else DECLINE_FUNDS


async def _site_message(view: _View, kind, request_id, **details) -> None:
    """Leave a message for the next review. That review decides any reply or buyer."""
    await wallet.record_event(
        view.conn,
        view.run_id,
        "site_message",
        view.run["current_window"],
        {
            "site_id": view.site_id,
            "site_name": view.site["name"],
            "message_kind": kind,
            "request_id": request_id,
            **details,
        },
        f"web:message:{view.owner_id}:{request_id}",
        view.owner_id,
    )


async def _site_event(view: _View, kind, key, **details) -> None:
    await wallet.record_event(
        view.conn,
        view.run_id,
        kind,
        view.run["current_window"],
        {"site_id": view.site_id, "site_name": view.site["name"], **details},
        key,
        view.owner_id,
    )


async def _add_to_cart(view: _View, form, request_id) -> dict:
    item = next((i for i in view.spec["items"] if i["id"] == form["item_id"]), None)
    if item is None:
        raise SiteError("We can't find that item.", 404)
    stock = await _inventory(view, "item")
    current = await _one(
        view.conn,
        """SELECT quantity FROM web_carts WHERE run_id=%s AND owner_id=%s AND site_id=%s
           AND item_id=%s""",
        (view.run_id, view.owner_id, view.site_id, item["id"]),
    )
    total = form["quantity"] + (current["quantity"] if current else 0)
    if total > min(stock.get(item["id"], 0), 99):
        left = stock.get(item["id"], 0)
        message = (
            f"Sorry, only {left} of {item['title']} left."
            if left
            else (f"Sorry, {item['title']} is sold out.")
        )
        return _result(await _cart_page(view, message), "rejected")
    await view.conn.execute(
        """INSERT INTO web_carts(run_id,owner_id,site_id,item_id,quantity)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (run_id,owner_id,site_id,item_id)
           DO UPDATE SET quantity=EXCLUDED.quantity""",
        (view.run_id, view.owner_id, view.site_id, item["id"], total),
    )
    return _result(
        await _cart_page(view, f"Added {form['quantity']} x {item['title']} to your cart.")
    )


async def _remove_from_cart(view: _View, form, request_id) -> dict:
    current = await _one(
        view.conn,
        """SELECT quantity FROM web_carts WHERE run_id=%s AND owner_id=%s AND site_id=%s
           AND item_id=%s""",
        (view.run_id, view.owner_id, view.site_id, form["item_id"]),
    )
    if current is None:
        return _result(await _cart_page(view, "That item is not in your cart."), "rejected")
    left = current["quantity"] - form.get("quantity", current["quantity"])
    if left > 0:
        await view.conn.execute(
            """UPDATE web_carts SET quantity=%s WHERE run_id=%s AND owner_id=%s AND site_id=%s
               AND item_id=%s""",
            (left, view.run_id, view.owner_id, view.site_id, form["item_id"]),
        )
    else:
        await view.conn.execute(
            "DELETE FROM web_carts WHERE run_id=%s AND owner_id=%s AND site_id=%s AND item_id=%s",
            (view.run_id, view.owner_id, view.site_id, form["item_id"]),
        )
    return _result(await _cart_page(view, "Your cart was updated."))


async def _schedule_order_update(view: _View, order_id, lines, eta_s) -> str | None:
    """Queue the delivery mail as a later plan. A run with no clock has no time to send it."""
    if not await _one(view.conn, "SELECT 1 FROM clock_state WHERE run_id=%s", (view.run_id,)):
        return None
    summary = ", ".join(f"{line['quantity']} x {line['title']}" for line in lines)
    key = f"web-order-update:{order_id}"
    body = (
        f"Your order {order_id} was delivered: {summary}. "
        f"Thank you for shopping with {view.site['name']}."
    )
    mail = {
        "to": view.owner_id,
        "from_name": view.site["name"][:120],
        "subject": f"Your order {order_id} has arrived",
        "body": body[:6000],
    }
    await clock.schedule(
        view.conn,
        view.run_id,
        eta_s,
        "director_plan",
        payload={"mail": [mail], "about_owner": view.owner_id},
        key=key,
        owner_id=view.owner_id,
        source="web",
    )
    return key


async def _checkout(view: _View, form, request_id) -> dict:
    cart = await _rows(
        view.conn,
        """SELECT c.item_id,c.quantity,i.remaining FROM web_carts c
           LEFT JOIN web_inventory i ON i.run_id=c.run_id AND i.site_id=c.site_id
           AND i.entry_kind='item' AND i.entry_id=c.item_id
           WHERE c.run_id=%s AND c.owner_id=%s AND c.site_id=%s ORDER BY c.item_id""",
        (view.run_id, view.owner_id, view.site_id),
    )
    if not cart:
        return _result(await _cart_page(view, "Your cart is empty."), "rejected")
    items = {entry["id"]: entry for entry in view.spec["items"]}
    lines = []
    for line in cart:
        item = items.get(line["item_id"])
        if item is None or (line["remaining"] or 0) < line["quantity"]:
            title = item["title"] if item else line["item_id"]
            left = line["remaining"] or 0
            message = (
                f"Sorry, only {left} of {title} left. Change your cart and try again."
                if left
                else f"Sorry, {title} is sold out. Change your cart and try again."
            )
            return _result(await _cart_page(view, message), "rejected")
        lines.append(
            {
                "item_id": item["id"],
                "title": item["title"],
                "quantity": line["quantity"],
                "unit_price": item["price"],
                "line_total": item["price"] * line["quantity"],
            }
        )
    subtotal = sum(line["line_total"] for line in lines)
    fee = view.spec["fees"]["delivery"]
    total = subtotal + fee
    order_id = _stable_id("ORD", view.run_id, view.owner_id, request_id)
    payment = await wallet.spend(
        view.conn,
        view.run_id,
        view.owner_id,
        total,
        f"{view.site['name']} order {order_id}",
        f"web:order:{order_id}",
        f"web:{view.site_id}",
    )
    if payment["status"] == "declined":
        message = await _decline(view, "checkout", total, payment, request_id, order_id)
        return _result(await _cart_page(view, message), "declined")
    for line in lines:
        await view.conn.execute(
            """UPDATE web_inventory SET remaining=remaining-%s WHERE run_id=%s AND site_id=%s
               AND entry_kind='item' AND entry_id=%s""",
            (line["quantity"], view.run_id, view.site_id, line["item_id"]),
        )
    eta_s = view.now_s + view.spec["eta_minutes"] * 60
    update_key = await _schedule_order_update(view, order_id, lines, eta_s)
    await view.conn.execute(
        """INSERT INTO web_orders(run_id,order_id,owner_id,site_id,lines,subtotal,delivery_fee,
                  total,note,eta_s,update_key,request_id,sim_s)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            view.run_id,
            order_id,
            view.owner_id,
            view.site_id,
            Jsonb(lines),
            subtotal,
            fee,
            total,
            form.get("note"),
            eta_s,
            update_key,
            request_id,
            view.now_s,
        ),
    )
    await view.conn.execute(
        "DELETE FROM web_carts WHERE run_id=%s AND owner_id=%s AND site_id=%s",
        (view.run_id, view.owner_id, view.site_id),
    )
    await _site_event(
        view,
        "web_order_placed",
        f"web:order:{order_id}",
        order_id=order_id,
        total=total,
        lines=lines,
        eta_s=eta_s,
    )
    page = await _order_page(view, order_id, f"Thank you. Your order {order_id} is confirmed.")
    return _result(page)


async def _cancel_order(view: _View, form, request_id) -> dict:
    order = await _one(
        view.conn,
        "SELECT * FROM web_orders WHERE run_id=%s AND owner_id=%s AND site_id=%s AND order_id=%s",
        (view.run_id, view.owner_id, view.site_id, form["order_id"]),
    )
    if order is None:
        raise SiteError("We can't find that order.", 404)
    if order["status"] == "cancelled":
        return _result(await _order_page(view, order["order_id"], "This order is cancelled."))
    if view.now_s >= order["eta_s"]:
        message = "This order was already delivered, so it can't be cancelled."
        return _result(await _order_page(view, order["order_id"], message), "rejected")
    await view.conn.execute(
        "UPDATE web_orders SET status='cancelled' WHERE run_id=%s AND order_id=%s",
        (view.run_id, order["order_id"]),
    )
    for line in order["lines"]:
        await view.conn.execute(
            """UPDATE web_inventory SET remaining=remaining+%s WHERE run_id=%s AND site_id=%s
               AND entry_kind='item' AND entry_id=%s""",
            (line["quantity"], view.run_id, view.site_id, line["item_id"]),
        )
    if order["update_key"]:
        await clock.cancel(view.conn, view.run_id, order["update_key"])
    if order["total"]:
        await wallet.adjust(
            view.conn,
            view.run_id,
            view.owner_id,
            order["total"],
            f"Refund for {view.site['name']} order {order['order_id']}",
            f"web:refund:{order['order_id']}",
            f"web:{view.site_id}",
        )
    await _site_event(
        view,
        "web_order_cancelled",
        f"web:order-cancelled:{order['order_id']}",
        order_id=order["order_id"],
        total=order["total"],
    )
    message = f"Your order was cancelled. {_money(order['total'])} goes back to your saved card."
    return _result(await _order_page(view, order["order_id"], message))


async def _create_listing(view: _View, form, request_id) -> dict:
    count = await _one(
        view.conn,
        "SELECT count(*) AS n FROM web_listings WHERE run_id=%s AND owner_id=%s",
        (view.run_id, view.owner_id),
    )
    if count["n"] >= MAX_LISTINGS_PER_OWNER:
        message = "You have reached the listing limit on this account."
        return _result(await _listings_page(view, {}, message), "rejected")
    listing_id = _stable_id("LST", view.run_id, view.owner_id, request_id)
    await view.conn.execute(
        """INSERT INTO web_listings(run_id,listing_id,owner_id,site_id,title,description,category,
                  price,request_id,sim_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            view.run_id,
            listing_id,
            view.owner_id,
            view.site_id,
            form["title"],
            form.get("description", ""),
            form.get("category", ""),
            form["price"],
            request_id,
            view.now_s,
        ),
    )
    await _site_message(
        view,
        "listing",
        request_id,
        listing_id=listing_id,
        title=form["title"],
        price=form["price"],
        body=form.get("description", ""),
    )
    return _result(await _listing_page(view, listing_id, "Your listing is live."))


async def _own_listing(view: _View, listing_id) -> dict:
    listing = await _one(
        view.conn,
        "SELECT * FROM web_listings WHERE run_id=%s AND owner_id=%s AND site_id=%s "
        "AND listing_id=%s",
        (view.run_id, view.owner_id, view.site_id, listing_id),
    )
    if listing is None:
        raise SiteError("We can't find that listing on your account.", 404)
    return listing


async def _update_listing(view: _View, form, request_id) -> dict:
    listing = await _own_listing(view, form["listing_id"])
    if listing["status"] != "active":
        message = "This listing is closed and can't be changed."
        return _result(await _listing_page(view, listing["listing_id"], message), "rejected")
    changes = {key: form.get(key, listing[key]) for key in ("title", "price", "description")}
    await view.conn.execute(
        """UPDATE web_listings SET title=%s,price=%s,description=%s,version=version+1
           WHERE run_id=%s AND listing_id=%s""",
        (
            changes["title"],
            changes["price"],
            changes["description"],
            view.run_id,
            listing["listing_id"],
        ),
    )
    await _site_event(
        view,
        "web_listing_changed",
        f"web:listing:{view.owner_id}:{request_id}",
        listing_id=listing["listing_id"],
        change="updated",
        **changes,
    )
    return _result(await _listing_page(view, listing["listing_id"], "Your listing was updated."))


async def _remove_listing(view: _View, form, request_id) -> dict:
    listing = await _own_listing(view, form["listing_id"])
    if listing["status"] == "active":
        await view.conn.execute(
            """UPDATE web_listings SET status='removed',version=version+1
               WHERE run_id=%s AND listing_id=%s""",
            (view.run_id, listing["listing_id"]),
        )
        await _site_event(
            view,
            "web_listing_changed",
            f"web:listing:{view.owner_id}:{request_id}",
            listing_id=listing["listing_id"],
            change="removed",
        )
    return _result(await _listings_page(view, {}, "Your listing was removed."))


async def _apply(view: _View, form, request_id) -> dict:
    posting = next((p for p in view.spec["postings"] if p["id"] == form["posting_id"]), None)
    if posting is None:
        raise SiteError("We can't find that job.", 404)
    prior = await _one(
        view.conn,
        """SELECT 1 FROM web_applications WHERE run_id=%s AND owner_id=%s AND site_id=%s
           AND posting_id=%s""",
        (view.run_id, view.owner_id, view.site_id, posting["id"]),
    )
    if prior is not None:
        message = "You already applied for this job."
        return _result(await _posting_page(view, posting["id"], message), "rejected")
    application_id = _stable_id("APP", view.run_id, view.owner_id, request_id)
    await view.conn.execute(
        """INSERT INTO web_applications(run_id,application_id,owner_id,site_id,posting_id,message,
                  request_id,sim_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            view.run_id,
            application_id,
            view.owner_id,
            view.site_id,
            posting["id"],
            form["message"],
            request_id,
            view.now_s,
        ),
    )
    await _site_message(
        view,
        "application",
        request_id,
        application_id=application_id,
        posting_id=posting["id"],
        title=posting["title"],
        body=form["message"],
    )
    message = "Your application was sent. They will contact you if there is a next step."
    return _result(await _posting_page(view, posting["id"], message))


async def _pay_bill(view: _View, form, request_id) -> dict:
    bill = await _one(
        view.conn,
        "SELECT * FROM web_bills WHERE run_id=%s AND site_id=%s AND bill_id=%s AND owner_id=%s",
        (view.run_id, view.site_id, form["bill_id"], view.owner_id),
    )
    if bill is None:
        raise SiteError("We can't find that bill on your account.", 404)
    if bill["status"] == "paid":
        return _result(await _bills_page(view, "This bill is already paid."), "rejected")
    payment = await wallet.spend(
        view.conn,
        view.run_id,
        view.owner_id,
        bill["amount"],
        f"{view.site['name']} bill: {bill['title']}",
        f"web:bill:{view.site_id}:{bill['bill_id']}",
        f"web:{view.site_id}",
    )
    if payment["status"] == "declined":
        message = await _decline(
            view, "pay_bill", bill["amount"], payment, request_id, bill["bill_id"]
        )
        return _result(await _bills_page(view, message), "declined")
    await view.conn.execute(
        """UPDATE web_bills SET status='paid',paid_request=%s,paid_sim_s=%s
           WHERE run_id=%s AND site_id=%s AND bill_id=%s""",
        (request_id, view.now_s, view.run_id, view.site_id, bill["bill_id"]),
    )
    await _site_event(
        view,
        "web_bill_paid",
        f"web:bill:{view.site_id}:{bill['bill_id']}",
        bill_id=bill["bill_id"],
        amount=bill["amount"],
    )
    message = f"Thank you. Your payment of {_money(bill['amount'])} was received."
    return _result(await _bills_page(view, message))


async def _book(view: _View, form, request_id) -> dict:
    slot = next((s for s in view.spec["slots"] if s["id"] == form["slot_id"]), None)
    if slot is None:
        raise SiteError("We can't find that time.", 404)
    prior = await _one(
        view.conn,
        """SELECT 1 FROM web_bookings WHERE run_id=%s AND owner_id=%s AND site_id=%s
           AND slot_id=%s AND status='booked'""",
        (view.run_id, view.owner_id, view.site_id, slot["id"]),
    )
    if prior is not None:
        return _result(await _bookings_page(view, "You already booked this time."), "rejected")
    places = await _inventory(view, "slot")
    if places.get(slot["id"], 0) < 1:
        return _result(await _slots_page(view, "Sorry, that time is now full."), "rejected")
    booking_id = _stable_id("BKG", view.run_id, view.owner_id, request_id)
    payment = await wallet.spend(
        view.conn,
        view.run_id,
        view.owner_id,
        slot["price"],
        f"{view.site['name']} booking: {slot['title']}",
        f"web:booking:{booking_id}",
        f"web:{view.site_id}",
    )
    if payment["status"] == "declined":
        message = await _decline(view, "book", slot["price"], payment, request_id, slot["id"])
        return _result(await _slots_page(view, message), "declined")
    await view.conn.execute(
        """UPDATE web_inventory SET remaining=remaining-1 WHERE run_id=%s AND site_id=%s
           AND entry_kind='slot' AND entry_id=%s""",
        (view.run_id, view.site_id, slot["id"]),
    )
    await view.conn.execute(
        """INSERT INTO web_bookings(run_id,booking_id,owner_id,site_id,slot_id,title,price,note,
                  request_id,sim_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            view.run_id,
            booking_id,
            view.owner_id,
            view.site_id,
            slot["id"],
            slot["title"],
            slot["price"],
            form.get("note"),
            request_id,
            view.now_s,
        ),
    )
    await _site_event(
        view,
        "web_booked",
        f"web:booking:{booking_id}",
        booking_id=booking_id,
        slot_id=slot["id"],
        title=slot["title"],
        price=slot["price"],
    )
    return _result(await _bookings_page(view, f"You are booked. Your reference is {booking_id}."))


async def _cancel_booking(view: _View, form, request_id) -> dict:
    booking = await _one(
        view.conn,
        "SELECT * FROM web_bookings WHERE run_id=%s AND owner_id=%s AND site_id=%s "
        "AND booking_id=%s",
        (view.run_id, view.owner_id, view.site_id, form["booking_id"]),
    )
    if booking is None:
        raise SiteError("We can't find that booking.", 404)
    if booking["status"] == "cancelled":
        return _result(await _bookings_page(view, "This booking is already cancelled."))
    await view.conn.execute(
        "UPDATE web_bookings SET status='cancelled' WHERE run_id=%s AND booking_id=%s",
        (view.run_id, booking["booking_id"]),
    )
    await view.conn.execute(
        """UPDATE web_inventory SET remaining=remaining+1 WHERE run_id=%s AND site_id=%s
           AND entry_kind='slot' AND entry_id=%s""",
        (view.run_id, view.site_id, booking["slot_id"]),
    )
    if booking["price"]:
        await wallet.adjust(
            view.conn,
            view.run_id,
            view.owner_id,
            booking["price"],
            f"Refund for {view.site['name']} booking {booking['booking_id']}",
            f"web:booking-refund:{booking['booking_id']}",
            f"web:{view.site_id}",
        )
    await _site_event(
        view,
        "web_booking_cancelled",
        f"web:booking-cancelled:{booking['booking_id']}",
        booking_id=booking["booking_id"],
        price=booking["price"],
    )
    return _result(await _bookings_page(view, "Your booking was cancelled."))


async def _contact(view: _View, form, request_id) -> dict:
    await _site_message(
        view,
        "contact",
        request_id,
        subject=form.get("subject", ""),
        body=form["message"],
        listing_id=form.get("listing_id"),
        posting_id=form.get("posting_id"),
    )
    return _result(await _contact_page(view, "Thanks. We received your message."))


_WRITE_HANDLERS = {
    "add_to_cart": _add_to_cart,
    "remove_from_cart": _remove_from_cart,
    "checkout": _checkout,
    "cancel_order": _cancel_order,
    "create_listing": _create_listing,
    "update_listing": _update_listing,
    "remove_listing": _remove_listing,
    "apply": _apply,
    "pay_bill": _pay_bill,
    "book": _book,
    "cancel_booking": _cancel_booking,
    "contact": _contact,
}


async def _read_action(view: _View, action, form) -> dict:
    if action == "search":
        if view.has("items"):
            return _result(await _items_page(view, {"q": form["q"]}))
        if view.kind == "marketplace":
            return _result(await _listings_page(view, {"q": form["q"]}))
        found = await _search_entries(view, form["q"])
        return _result(found)
    if action == "view_cart":
        return _result(await _cart_page(view))
    if action == "order_status":
        if "order_id" in form:
            page = await _order_page(view, form["order_id"])
            if page is None:
                raise SiteError("We can't find that order.", 404)
            return _result(page)
        return _result(await _orders_page(view))
    if action == "view_listings":
        return _result(await _listings_page(view, {}))
    return _result(await _bills_page(view))


async def _search_entries(view: _View, query: str) -> Page:
    lines = []
    for posting in view.spec["postings"]:
        if _matches(query, posting["title"], posting["description"], posting["location"]):
            lines.append(f"- {posting['title']} | {view.url('/postings/' + posting['id'])}")
    for slot in view.spec["slots"]:
        if _matches(query, slot["title"], slot["description"], slot["when"]):
            lines.append(f"- {slot['title']} | {slot['when']} | slot_id: {slot['id']}")
    section = Section(
        heading=f'Results for "{_short(query, 200)}"', lines=lines or ["Nothing found."]
    )
    return _page(view, "/", "Search", [section])


async def submit_action(conn, run_id, owner_id, site_id, action, fields, request_id) -> dict:
    """Run one form action and return its page as outcome, status, url, title and text.

    Outcome is done, declined or rejected. A write saves its result in the same transaction
    as its effects, so a retry with the same request ID returns that result and changes
    nothing. A reused ID with a changed form raises WorldError 409. A read saves nothing.
    A site with a built module declares its own actions, and every one of them saves a result.
    """
    built = await _one(
        conn,
        "SELECT 1 FROM web_sites WHERE run_id=%s AND site_id=%s AND spec ? 'module'",
        (run_id, site_id),
    )
    if built is not None:
        return await _submit_module_action(
            conn, run_id, owner_id, site_id, action, fields, request_id
        )
    if action not in READ_ACTIONS and action not in WRITE_ACTIONS:
        raise SiteError("This site does not offer that action.", 422)
    if not isinstance(fields, dict) or len(fields) > 12:
        raise SiteError("Please check the form fields.", 422)
    if action in READ_ACTIONS:
        form = _validate_form(READ_ACTIONS[action], fields)
        view = await _load_view(conn, run_id, site_id, owner_id)
        if view is None:
            raise SiteError("Site not found.", 404)
        if action not in _available_actions(view.spec):
            raise SiteError("This site does not offer that action.", 422)
        return await _read_action(view, action, form)
    if not isinstance(request_id, str) or not _REQUEST_RE.fullmatch(request_id):
        raise SiteError("A valid request ID is required.", 422)
    form = _validate_form(WRITE_ACTIONS[action], fields)
    payload = {"site_id": site_id, "action": action, "fields": form}
    digest = _hash(payload)
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        prior = await _one(
            conn,
            """SELECT payload_hash,result FROM web_requests
               WHERE run_id=%s AND owner_id=%s AND request_id=%s""",
            (run_id, owner_id, request_id),
        )
        if prior is not None:
            if prior["payload_hash"] != digest:
                raise SiteError("This request ID was already used for a different form.")
            return prior["result"]
        _WORLD._active(run)
        await _WORLD._account(conn, run_id, owner_id)
        site = await _one(
            conn, "SELECT * FROM web_sites WHERE run_id=%s AND site_id=%s", (run_id, site_id)
        )
        if site is None:
            raise SiteError("Site not found.", 404)
        if action not in _available_actions(site["spec"]):
            raise SiteError("This site does not offer that action.", 422)
        now_s = await wallet.sim_now(conn, run_id)
        view = _View(conn, run, site, site["spec"], owner_id, now_s)
        result = await _WRITE_HANDLERS[action](view, form, request_id)
        await conn.execute(
            """INSERT INTO web_requests(run_id,owner_id,request_id,payload_hash,payload,result,
                      sim_s) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (run_id, owner_id, request_id, digest, Jsonb(payload), Jsonb(result), now_s),
        )
        return result
