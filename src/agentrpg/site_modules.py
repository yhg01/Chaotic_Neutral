"""Built site modules: small pure programs that give an invented website its own forms.

A module is Python source that is written and checked before a scenario is frozen. It holds
an ACTIONS declaration and two pure functions, page and act. It never receives a database
connection, a clock, a file or the network. The engine gives it one visitor's view of the
site state and takes back a page description or a bounded change. Code here checks the
source, runs it with a short list of builtins and a step limit, and validates every value
it returns. Money, mail and messages are typed effects that the caller applies.

The source check and the builtin list are a second barrier, not a security boundary. The
first barrier is that a person reads a module before a scenario that holds it is used.
docs/WORLD_BUILD.md is the contract.
"""

import ast
import builtins
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

MAX_SOURCE_CHARS = 40_000
MAX_STATE_CHARS = 200_000
MAX_ACTIONS = 24
MAX_FIELDS = 12
MAX_SECTIONS = 20
MAX_LINES = 200
MAX_LINE_CHARS = 1000
MAX_RECORDS = 500
MAX_INT = 100_000_000
STEP_LIMIT = 200_000
CRAWL_LIMIT = 60

ACTION_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
RECORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
PATH_RE = re.compile(r"/[A-Za-z0-9_./-]{0,199}")
# A page reaches the assistant, so it cannot talk about the test around it.
TEST_WORDS = re.compile(r"\b(simulat\w*|scenario\w*)\b", re.IGNORECASE)

STATE_PARTS = ("public", "accounts", "records")
OUTCOMES = ("done", "rejected")
RESULT_KEYS = {"outcome", "notice", "path", "query", "public", "account", "records", "effects"}


class ModuleProblem(ValueError):
    """Source, a declaration or a state document that breaks the contract."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


class ModuleRunError(RuntimeError):
    """A module function raised, ran too long or returned a value outside the contract."""


class FieldProblem(ValueError):
    """A form the visitor filled in wrongly. The message is safe to show on the page."""


class _StepLimit(BaseException):
    """Module code cannot catch this, because the source check allows no try statement."""


# ---------------------------------------------------------------------------
# Source check
# ---------------------------------------------------------------------------

_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.For, ast.If, ast.Expr, ast.Pass, ast.Break, ast.Continue, ast.BoolOp, ast.BinOp,
    ast.UnaryOp, ast.IfExp, ast.Dict, ast.Set, ast.List, ast.Tuple, ast.ListComp, ast.SetComp,
    ast.DictComp, ast.GeneratorExp, ast.comprehension, ast.Compare, ast.Call, ast.keyword,
    ast.JoinedStr, ast.FormattedValue, ast.Constant, ast.Attribute, ast.Subscript, ast.Slice,
    ast.Name, ast.Load, ast.Store, ast.Lambda, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
)  # fmt: skip
# Methods of text, lists, dicts and sets. Nothing else can follow a dot.
_ATTRIBUTES = frozenset(
    """lower upper strip lstrip rstrip split rsplit splitlines join replace startswith endswith
    find index count isdigit isalpha isalnum isspace title capitalize zfill ljust rjust
    partition rpartition removeprefix removesuffix casefold append extend insert pop remove
    sort reverse copy clear get items keys values setdefault update add discard union
    intersection difference issubset issuperset""".split()
)
_BUILTINS = {
    name: getattr(builtins, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "int",
        "isinstance", "len", "list", "map", "max", "min", "range", "reversed", "round", "set",
        "sorted", "str", "sum", "tuple", "zip",
    )
}  # fmt: skip


def _constants(tree) -> list[str]:
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def check_source(source) -> list[str]:
    """Return every problem with a module's source. An empty list means it may be loaded."""
    if not isinstance(source, str) or not source.strip():
        return ["The module has no source."]
    if len(source) > MAX_SOURCE_CHARS:
        return [f"The module is longer than {MAX_SOURCE_CHARS} characters."]
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        line = getattr(error, "lineno", None)
        return [f"The module is not valid Python (line {line})."]
    problems: list[str] = []

    def bad(node, text):
        problems.append(f"Line {getattr(node, 'lineno', '?')}: {text}")

    functions = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            functions[node.name] = node
            if node.decorator_list or node.returns is not None:
                bad(node, "a function takes no decorator and no return annotation.")
            if any(isinstance(inner, ast.FunctionDef) for inner in ast.walk(node) if inner != node):
                bad(node, "a function cannot define another function.")
        elif isinstance(node, ast.Assign):
            if not all(isinstance(t, ast.Name) and t.id.isupper() for t in node.targets):
                bad(node, "a top-level value needs one name in capital letters.")
        elif not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bad(node, "the top level holds only values in capital letters and functions.")
    for node in ast.walk(tree):
        if not isinstance(node, _NODES):
            bad(node, f"{type(node).__name__} is not allowed.")
        elif isinstance(node, ast.Attribute) and node.attr not in _ATTRIBUTES:
            bad(node, f"the attribute .{node.attr} is not allowed.")
        elif isinstance(node, ast.Name) and node.id.startswith("_"):
            bad(node, "a name cannot start with an underscore.")
        elif isinstance(node, ast.arg) and (node.arg.startswith("_") or node.annotation):
            bad(node, "an argument takes no underscore prefix and no annotation.")
        elif isinstance(node, ast.arguments) and (node.vararg or node.kwarg):
            bad(node, "a function takes named arguments only.")
        elif isinstance(node, ast.keyword) and node.arg is None:
            bad(node, "a call cannot unpack keyword arguments.")
        elif isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or value is None or isinstance(value, str):
                continue
            if not isinstance(value, int) or abs(value) > MAX_INT:
                bad(node, f"a number must be a whole number up to {MAX_INT}.")
    for name, count in (("page", 3), ("act", 3)):
        node = functions.get(name)
        if node is None:
            problems.append(f"The module must define {name}.")
        elif len(node.args.args) != count or node.args.defaults or node.args.kwonlyargs:
            problems.append(f"{name} takes exactly {count} arguments.")
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    if "ACTIONS" not in assigned:
        problems.append("The module must define ACTIONS.")
    found = TEST_WORDS.search("\n".join(_constants(tree)))
    if found:
        problems.append(f"Text must read as an ordinary website. Remove the word {found.group(0)}.")
    return problems[:40]


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


class _Decl(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
_Hint = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
_Choice = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class FieldDecl(_Decl):
    type: Literal["text", "int", "choice"] = "text"
    required: bool = False
    min: int = Field(default=0, ge=-MAX_INT, le=MAX_INT)
    max: int | None = Field(default=None, ge=0, le=MAX_INT)
    choices: tuple[_Choice, ...] = Field(default=(), max_length=40)
    hint: _Hint = ""

    def describe(self) -> str:
        if self.type == "int":
            kind = "a whole number"
        elif self.type == "choice":
            kind = "one of: " + ", ".join(self.choices)
        else:
            kind = ""
        return ", ".join(part for part in (kind, self.hint) if part)


class ActionDecl(_Decl):
    label: _Label
    write: bool = True
    fields: dict[str, FieldDecl] = Field(default_factory=dict, max_length=MAX_FIELDS)


def _declare(raw) -> dict[str, ActionDecl]:
    if not isinstance(raw, dict) or not 1 <= len(raw) <= MAX_ACTIONS:
        raise ModuleProblem(f"ACTIONS must hold 1 to {MAX_ACTIONS} actions.")
    actions, problems = {}, []
    for name, value in raw.items():
        if not isinstance(name, str) or not ACTION_RE.fullmatch(name):
            problems.append(f"The action name {name!r} must be lower case words with underscores.")
            continue
        try:
            decl = ActionDecl.model_validate(value)
        except ValidationError as error:
            first = error.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            problems.append(f"Action {name}: {where}: {first['msg']}")
            continue
        for field_name, item in decl.fields.items():
            if not ACTION_RE.fullmatch(field_name):
                problems.append(f"Action {name}: the field name {field_name!r} is not valid.")
            if item.type == "choice" and not item.choices:
                problems.append(f"Action {name}: the choice field {field_name} needs choices.")
        actions[name] = decl
    if problems:
        raise ModuleProblem(problems)
    return actions


def _nav(raw) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or len(raw) > 12:
        raise ModuleProblem("NAV must be a list of up to 12 links.")
    links = []
    for item in raw:
        label, path = (item.get("label"), item.get("path")) if isinstance(item, dict) else (0, 0)
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 or not _is_path(path):
            raise ModuleProblem("Each NAV link needs a label and a path that starts with /.")
        links.append((label.strip(), path))
    return tuple(links)


def _is_path(value) -> bool:
    return isinstance(value, str) and PATH_RE.fullmatch(value) is not None and ".." not in value


# ---------------------------------------------------------------------------
# Loading and running
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Module:
    sha256: str
    filename: str
    actions: dict[str, ActionDecl]
    nav: tuple[tuple[str, str], ...]
    functions: dict = field(repr=False, compare=False, default_factory=dict)


_LOADED: dict[str, Module] = {}


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()


def _guarded(filename: str, function, *args):
    """Call module code with a limit on executed lines, so a loop cannot hold the engine."""
    steps = 0

    def trace(frame, event, _arg):
        nonlocal steps
        if frame.f_code.co_filename != filename:
            return None
        steps += 1
        if steps > STEP_LIMIT:
            raise _StepLimit
        return trace

    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        return function(*args)
    except _StepLimit:
        raise ModuleRunError("The module ran for too long.") from None
    except RecursionError:
        raise ModuleRunError("The module called itself too deeply.") from None
    except Exception as error:
        detail = f"{type(error).__name__}: {str(error)[:200]}"
        raise ModuleRunError(f"The module raised {detail}") from None
    finally:
        sys.settrace(previous)


def load(source: str) -> Module:
    """Check and load one module. The same source returns the same loaded module."""
    problems = check_source(source)
    if problems:
        raise ModuleProblem(problems)
    digest = source_hash(source)
    if digest in _LOADED:
        return _LOADED[digest]
    filename = f"<site-module {digest[:12]}>"
    scope: dict[str, Any] = {"__builtins__": dict(_BUILTINS)}
    try:
        _guarded(filename, exec, compile(source, filename, "exec"), scope)
    except ModuleRunError as error:
        raise ModuleProblem(f"The module does not load. {error}") from None
    module = Module(
        sha256=digest,
        filename=filename,
        actions=_declare(scope.get("ACTIONS")),
        nav=_nav(scope.get("NAV")),
        functions={"page": scope["page"], "act": scope["act"]},
    )
    if len(_LOADED) > 200:
        _LOADED.clear()
    _LOADED[digest] = module
    return module


def _plain(value, what: str):
    """Copy a value through JSON, so module code and the engine never share an object."""
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError):
        raise ModuleRunError(f"{what} must be plain data: text, whole numbers and lists.") from None


# ---------------------------------------------------------------------------
# State: what is public, what belongs to one visitor, and records with an access list
# ---------------------------------------------------------------------------


def check_state(state, owners=None) -> dict:
    """Return a complete state document, or raise ModuleProblem.

    owners, when given, is the set of people who may hold an account or appear in an
    access list.
    """
    if state is None:
        state = {}
    if not isinstance(state, dict) or set(state) - set(STATE_PARTS):
        raise ModuleProblem("Site state holds only public, accounts and records.")
    try:
        document = json.loads(json.dumps(state, allow_nan=False))
    except (TypeError, ValueError):
        raise ModuleProblem("Site state must be plain data.") from None
    result = {part: document.get(part) or {} for part in STATE_PARTS}
    if not all(isinstance(result[part], dict) for part in STATE_PARTS):
        raise ModuleProblem("Each part of site state must be an object.")
    if len(result["records"]) > MAX_RECORDS:
        raise ModuleProblem(f"Site state holds at most {MAX_RECORDS} records.")
    for owner_id, account in result["accounts"].items():
        if not isinstance(account, dict):
            raise ModuleProblem("An account must be an object.")
        if owners is not None and owner_id not in owners:
            raise ModuleProblem(f"Site state names an unknown person: {owner_id}.")
    for record_id, record in result["records"].items():
        if not RECORD_RE.fullmatch(record_id):
            raise ModuleProblem(f"The record ID {record_id!r} is not valid.")
        if (
            not isinstance(record, dict)
            or set(record) != {"access", "data"}
            or not isinstance(record["data"], dict)
            or not isinstance(record["access"], list)
            or not all(isinstance(name, str) for name in record["access"])
        ):
            raise ModuleProblem(f"Record {record_id} needs an access list and a data object.")
        if owners is not None and not set(record["access"]) <= set(owners):
            raise ModuleProblem(f"Record {record_id} names an unknown person.")
    if len(json.dumps(result)) > MAX_STATE_CHARS:
        raise ModuleProblem(f"Site state is larger than {MAX_STATE_CHARS} characters.")
    return result


def view_for(state: dict, viewer: str | None, context: dict | None = None) -> dict:
    """Build what one visitor's request may read. Other people's parts are not in it."""
    context = context or {}
    return {
        "viewer": viewer,
        "viewer_name": context.get("viewer_name", "") if viewer else "",
        "minutes": int(context.get("minutes", 0)),
        "now": context.get("now", ""),
        "date": context.get("date", ""),
        "weekday": context.get("weekday", ""),
        "public": state["public"],
        "account": state["accounts"].get(viewer, {}) if viewer else {},
        "records": {
            record_id: record["data"]
            for record_id, record in state["records"].items()
            if viewer is not None and viewer in record["access"]
        },
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _line(value, limit, what) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ModuleRunError(f"{what} must be text.")
    text = str(value)
    if any(
        (ord(char) < 32 and char not in "\n\t") or 0xD800 <= ord(char) <= 0xDFFF for char in text
    ):
        raise ModuleRunError(f"{what} holds a character that a page cannot show.")
    return text[:limit]


def _page_data(module: Module, raw) -> dict:
    """Validate a page description and return it in one fixed shape."""
    raw = _plain(raw, "A page")
    if not isinstance(raw, dict) or set(raw) - {"title", "sections"}:
        raise ModuleRunError("A page is an object with title and sections.")
    sections = raw.get("sections", [])
    if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
        raise ModuleRunError(f"A page has at most {MAX_SECTIONS} sections.")
    result = {"title": _line(raw.get("title", ""), 160, "A page title").strip(), "sections": []}
    if not result["title"]:
        raise ModuleRunError("A page needs a title.")
    for section in sections:
        if not isinstance(section, dict) or set(section) - {"heading", "lines", "links", "forms"}:
            raise ModuleRunError("A section holds heading, lines, links and forms.")
        lines = section.get("lines", [])
        links = section.get("links", [])
        forms = section.get("forms", [])
        if not all(isinstance(part, list) for part in (lines, links, forms)):
            raise ModuleRunError("lines, links and forms must be lists.")
        if len(lines) > MAX_LINES or len(links) > 40 or len(forms) > 12:
            raise ModuleRunError("A section is too long.")
        clean = {
            "heading": _line(section.get("heading", ""), 160, "A heading"),
            "lines": [_line(line, MAX_LINE_CHARS, "A line") for line in lines],
            "links": [],
            "forms": [],
        }
        for link in links:
            if not isinstance(link, dict) or not _is_path(link.get("path")):
                raise ModuleRunError("A link needs a label and a path that starts with /.")
            clean["links"].append(
                {"label": _line(link.get("label", ""), 120, "A link label"), "path": link["path"]}
            )
        for form in forms:
            action = form.get("action") if isinstance(form, dict) else None
            if action not in module.actions or set(form) - {"action", "label", "values", "path"}:
                raise ModuleRunError("A form names one declared action.")
            values = form.get("values", {})
            if not isinstance(values, dict) or set(values) - set(module.actions[action].fields):
                raise ModuleRunError(f"The form {action} presets a field it does not declare.")
            path = form.get("path", "/")
            if not _is_path(path):
                raise ModuleRunError("A form path must start with /.")
            clean["forms"].append(
                {
                    "action": action,
                    "label": _line(
                        form.get("label") or module.actions[action].label, 80, "A label"
                    ),
                    "path": path,
                    "values": {
                        name: _line(value, 400, "A preset") for name, value in values.items()
                    },
                }
            )
        result["sections"].append(clean)
    return result


def render_page(module: Module, state, viewer, context, path, query=None) -> dict | None:
    """Ask the module for one page. None means the site has no such page."""
    view = _plain(view_for(state, viewer, context), "A view")
    raw = _guarded(module.filename, module.functions["page"], view, path, dict(query or {}))
    return None if raw is None else _page_data(module, raw)


def page_text(data: dict) -> str:
    """All words of a page, for checks."""
    parts = [data["title"]]
    for section in data["sections"]:
        parts.append(section["heading"])
        parts.extend(section["lines"])
        parts.extend(link["label"] for link in section["links"])
        parts.extend(form["label"] for form in section["forms"])
    return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def clean_fields(decl: ActionDecl, fields) -> dict:
    """Check a filled form against its declaration. FieldProblem names the wrong fields."""
    if not isinstance(fields, dict) or len(fields) > MAX_FIELDS:
        raise FieldProblem("Please check the form fields.")
    wrong = sorted(set(fields) - set(decl.fields))
    clean = {}
    for name, item in decl.fields.items():
        value = fields.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if item.required:
                wrong.append(name)
            continue
        if item.type == "int":
            if isinstance(value, str) and re.fullmatch(r"-?\d{1,12}", value.strip()):
                value = int(value.strip())
            upper = MAX_INT if item.max is None else item.max
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not item.min <= value <= upper
            ):
                wrong.append(name)
                continue
        else:
            value = str(value).strip() if isinstance(value, (str, int)) else None
            limit = 400 if item.max is None else min(item.max, 4000)
            if (
                isinstance(fields.get(name), bool)
                or value is None
                or len(value) > limit
                or any(ord(char) < 32 and char not in "\n\t" for char in value)
                or (item.type == "choice" and value not in item.choices)
            ):
                wrong.append(name)
                continue
        clean[name] = value
    if wrong:
        names = ", ".join(re.sub(r"[^\w-]", "", str(name))[:40] for name in sorted(set(wrong)))
        raise FieldProblem(f"Please check these form fields: {names}.")
    return clean


@dataclass
class ActionResult:
    outcome: str
    notice: str
    path: str
    state: dict
    query: dict = field(default_factory=dict)
    changed: bool = False
    charge: dict | None = None
    message: dict | None = None
    mail: list[dict] = field(default_factory=list)


def _effects(raw, strict: bool) -> tuple[dict | None, dict | None, list[dict]]:
    if not isinstance(raw, list) or len(raw) > 4:
        raise ModuleRunError("effects must be a list of at most 4 effects.")
    charge = message = None
    mail = []
    for effect in raw:
        kind = effect.get("type") if isinstance(effect, dict) else None
        if kind == "charge":
            amount = effect.get("amount")
            if charge is not None or set(effect) - {"type", "amount", "memo"}:
                raise ModuleRunError("An action makes at most one charge, with amount and memo.")
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or not 0 <= amount <= 1_000_000
            ):
                raise ModuleRunError("A charge needs a whole amount from 0 to 1,000,000.")
            memo = _line(effect.get("memo", ""), 200, "A charge memo").strip()
            if amount:
                charge = {"amount": amount, "memo": memo or "Payment"}
        elif kind == "message":
            if message is not None or set(effect) - {"type", "subject", "body"}:
                raise ModuleRunError("An action leaves at most one message, with subject and body.")
            body = _line(effect.get("body", ""), 4000, "A message body").strip()
            if not body:
                raise ModuleRunError("A message needs a body.")
            subject = _line(effect.get("subject", ""), 200, "A message subject").strip()
            message = {"subject": subject, "body": body}
        elif kind == "mail":
            minutes = effect.get("in_minutes", 1)
            if len(mail) == 2 or set(effect) - {"type", "subject", "body", "in_minutes"}:
                raise ModuleRunError("An action sends at most two letters to the visitor.")
            if (
                isinstance(minutes, bool)
                or not isinstance(minutes, int)
                or not 1 <= minutes <= 20160
            ):
                raise ModuleRunError("A letter arrives after 1 to 20160 minutes.")
            subject = _line(effect.get("subject", ""), 200, "A mail subject").strip()
            body = _line(effect.get("body", ""), 6000, "A mail body").strip()
            if not subject or not body:
                raise ModuleRunError("A letter needs a subject and a body.")
            mail.append({"subject": subject, "body": body, "in_minutes": minutes})
        else:
            raise ModuleRunError("An effect is a charge, a message or a mail.")
    if strict and TEST_WORDS.search(json.dumps([charge, message, mail])):
        raise ModuleRunError("Effect text must read as an ordinary website.")
    return charge, message, mail


def run_action(
    module: Module, state, viewer, context, action, fields, *, strict=False
) -> ActionResult:
    """Run one action for one visitor and return the validated result. Nothing is saved here.

    The caller cleans the fields first. A rejected result and a read action change nothing:
    any change they return is dropped, or raises ModuleRunError when strict is set, as the
    build checks do. The module cannot change another person's account, a record that the
    visitor cannot read, or any access list. A new record belongs to the visitor.
    """
    decl = module.actions[action]
    if viewer is None:
        raise ModuleRunError("An action needs a visitor.")
    view = _plain(view_for(state, viewer, context), "A view")
    raw = _guarded(module.filename, module.functions["act"], view, action, dict(fields))
    raw = _plain(raw, "An action result")
    if not isinstance(raw, dict) or set(raw) - RESULT_KEYS:
        raise ModuleRunError("An action returns an object with: " + ", ".join(sorted(RESULT_KEYS)))
    outcome = raw.get("outcome", "done")
    path = raw.get("path", "/")
    if outcome not in OUTCOMES or not _is_path(path):
        raise ModuleRunError("An action returns outcome done or rejected and a path.")
    notice = _line(raw.get("notice", ""), 500, "A notice").strip()
    query = raw.get("query") or {}
    if not isinstance(query, dict) or len(query) > 4:
        raise ModuleRunError("query must be an object with at most 4 values.")
    query = {str(name)[:40]: _line(value, 200, "A query value") for name, value in query.items()}
    if strict and TEST_WORDS.search(notice):
        raise ModuleRunError("A notice must read as an ordinary website.")
    changes = {
        key: raw[key] for key in ("public", "account", "records") if raw.get(key) is not None
    }
    effects = raw.get("effects") or []
    if (outcome == "rejected" or not decl.write) and (changes or effects):
        if strict:
            raise ModuleRunError("A rejected result or a read action must change nothing.")
        changes, effects = {}, []
    charge, message, mail = _effects(effects, strict)
    result = ActionResult(
        outcome, notice, path, state, query=query, charge=charge, message=message, mail=mail
    )
    if not changes:
        return result
    new_state = _plain(state, "Site state")
    for key in ("public", "account"):
        if key in changes and not isinstance(changes[key], dict):
            raise ModuleRunError(f"{key} must be an object.")
    if "public" in changes:
        new_state["public"] = changes["public"]
    if "account" in changes:
        new_state["accounts"][viewer] = changes["account"]
    records = changes.get("records", {})
    if not isinstance(records, dict):
        raise ModuleRunError("records must be an object.")
    for record_id, data in records.items():
        if not RECORD_RE.fullmatch(record_id) or not isinstance(data, dict):
            raise ModuleRunError("A record needs a valid ID and a data object.")
        known = new_state["records"].get(record_id)
        if known is None:
            new_state["records"][record_id] = {"access": [viewer], "data": data}
        elif viewer in known["access"]:
            known["data"] = data
        else:
            raise ModuleRunError("An action cannot change a record its visitor cannot read.")
    try:
        result.state = check_state(new_state)
    except ModuleProblem as error:
        raise ModuleRunError(str(error)) from None
    result.changed = result.state != state
    return result


# ---------------------------------------------------------------------------
# Fixed checks that every built module must pass
# ---------------------------------------------------------------------------

_SAMPLE_TEXT = ("A", "zz-unknown-0", "Jordan Example", "12", "x" * 60)


def _sample_fields(decl: ActionDecl, round_number: int) -> dict:
    fields = {}
    for index, (name, item) in enumerate(sorted(decl.fields.items())):
        if item.type == "int":
            upper = MAX_INT if item.max is None else item.max
            fields[name] = (item.min, upper, min(upper, item.min + 7))[round_number % 3]
        elif item.type == "choice":
            fields[name] = item.choices[round_number % len(item.choices)]
        else:
            limit = 400 if item.max is None else item.max
            fields[name] = _SAMPLE_TEXT[(round_number + index) % len(_SAMPLE_TEXT)][:limit] or "A"
    return fields


def check_module(source: str, state, owners) -> list[str]:
    """Run the fixed checks. An empty list means the module may go into a scenario.

    They cover the source rules, the declarations, every page a visitor can reach from the
    home page for each person and for a visitor with no account, the words on those pages,
    repeatable output, and every action with sample values. They do not show that the site
    does what its order asked. The order's own checks do that.
    """
    try:
        module = load(source)
        state = check_state(state, owners)
    except ModuleProblem as error:
        return error.problems
    problems: list[str] = []
    context = {"minutes": 0, "now": "2026-01-05T09:00", "date": "2026-01-05", "weekday": "Monday"}

    def crawl(viewer, current, label):
        seen, queue = set(), ["/", *(path for _, path in module.nav)]
        while queue and len(seen) < CRAWL_LIMIT:
            path = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            try:
                data = render_page(module, current, viewer, {**context, "viewer_name": "A"}, path)
                again = render_page(module, current, viewer, {**context, "viewer_name": "A"}, path)
            except ModuleRunError as error:
                problems.append(f"{label}: page {path} for {viewer or 'a visitor'}: {error}")
                continue
            if data is None:
                problems.append(f"{label}: the link {path} leads to no page.")
                continue
            if data != again:
                problems.append(f"{label}: page {path} differs between two identical requests.")
            found = TEST_WORDS.search(page_text(data))
            if found:
                problems.append(f"{label}: page {path} uses the word {found.group(0)}.")
            for section in data["sections"]:
                queue.extend(link["path"] for link in section["links"])
                queue.extend(form["path"] for form in section["forms"])

    people = [None, *sorted(owners)]
    for viewer in people:
        crawl(viewer, state, "Start")
        try:
            if render_page(module, state, viewer, context, "/no-such-page-here") is not None:
                problems.append("An unknown path must return None, so the visitor sees not found.")
        except ModuleRunError as error:
            problems.append(f"An unknown path for {viewer or 'a visitor'}: {error}")
    for viewer in sorted(owners):
        current = state
        for name, decl in module.actions.items():
            for round_number in range(3):
                fields = _sample_fields(decl, round_number)
                try:
                    result = run_action(
                        module, current, viewer, context, name, clean_fields(decl, fields),
                        strict=True,
                    )  # fmt: skip
                except FieldProblem:
                    problems.append(f"Action {name}: sample values break its own declaration.")
                    break
                except ModuleRunError as error:
                    problems.append(f"Action {name} with {json.dumps(fields)[:200]}: {error}")
                    break
                current = result.state
        crawl(viewer, current, "After sample actions")
    return list(dict.fromkeys(problems))[:30]
