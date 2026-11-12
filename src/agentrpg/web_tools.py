"""The assistant's web tools. They read and change only this owner's view of invented sites.

No request leaves the process. A URL host maps to a site in this run, and an unknown host
gets a plain not-found page. Results are short clean text: they hold no run identifier and
no test language. A model request for a new site runs with no database transaction open.
"""

from typing import Annotated, Literal
from uuid import UUID, uuid5

import psycopg
from psycopg_pool import PoolTimeout
from pydantic import Field, StringConstraints, ValidationError

from agentrpg import sim_web, wallet
from agentrpg.actor import ModelGenerationError
from agentrpg.agent_tools import _Arguments, _error
from agentrpg.sim_web import SiteSpec
from agentrpg.world import WorldError, _one

_Url = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
_RequestID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
_FieldValue = Annotated[str, StringConstraints(max_length=4000)] | int


class WebSearch(_Arguments):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class OpenPage(_Arguments):
    url: _Url


class SubmitForm(_Arguments):
    url: _Url = Field(description="The form's url, as the page shows it.")
    action: Literal[
        "search",
        "add_to_cart",
        "remove_from_cart",
        "view_cart",
        "checkout",
        "order_status",
        "cancel_order",
        "create_listing",
        "update_listing",
        "remove_listing",
        "view_listings",
        "apply",
        "view_bills",
        "pay_bill",
        "book",
        "cancel_booking",
        "contact",
    ] = Field(description="The form's action, as the page shows it.")
    fields: dict[str, _FieldValue] = Field(
        default_factory=dict, description="The form's fields, as the page shows them."
    )
    request_id: _RequestID | None = Field(
        default=None,
        description="Optional. Send the same value again only to retry a form that may not "
        "have gone through.",
    )


WEB_TOOLS = {
    "web_search": (WebSearch, "Search the web"),
    "open_page": (OpenPage, "Open a web page"),
    "submit_form": (SubmitForm, "Fill in and send a form on a page"),
}

_NOT_RUNNING = "The site is not available right now. Try again later."
_UNAVAILABLE = "The site could not be reached. Try again later."
_INTERNAL_ERRORS = {"The run is not running.", "Run not found.", "Owner not found in this world."}


def _page_data(page: sim_web.Page) -> dict:
    return {"url": page.url, "title": page.title, "text": sim_web.render_text(page)}


def _search_text(query: str, results: list[dict]) -> str:
    if not results:
        return f'No results for "{query}". Try different words.'
    lines = [f'Results for "{query}":']
    for number, result in enumerate(results, 1):
        lines.append(f"{number}. {result['name']}: {sim_web.site_url(result['site_id'])}")
        if result.get("tagline"):
            lines.append(f"   {result['tagline']}")
    return "\n".join(lines)


def _proposed_sites(plan) -> list:
    if plan is None:
        return []
    if isinstance(plan, SiteSpec):
        return [plan]
    sites = plan.get("sites") if isinstance(plan, dict) else getattr(plan, "sites", None)
    return list(sites or [])


class WebTools:
    """One owner's web tools for one run. The owner comes from the caller, not from a model."""

    def __init__(
        self,
        pool,
        run_id: UUID | str,
        owner_id: str,
        director_brain=None,
        *,
        operation_namespace=None,
    ) -> None:
        self.pool = pool
        self.run_id = UUID(str(run_id))
        self.owner_id = owner_id
        self.director_brain = director_brain
        if operation_namespace is not None and not isinstance(operation_namespace, UUID):
            raise ValueError("The operation namespace must be a UUID.")
        # A stable default keeps tool-call request IDs across a resume.
        self.operation_namespace = operation_namespace or uuid5(
            self.run_id, f"web-tools:{owner_id}"
        )
        self.tools = dict(WEB_TOOLS)

    def definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in self.tools.items()
        ]

    async def execute(self, name: str, arguments: dict, call_id: str) -> dict:
        if not isinstance(name, str) or name not in self.tools:
            return _error(422, "Unknown web tool.")
        if (
            not isinstance(call_id, str)
            or not call_id.strip()
            or len(call_id) > 512
            or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in call_id)
        ):
            return _error(422, "A valid tool call ID is required.")
        if not isinstance(arguments, dict):
            return _error(422, "Tool arguments must be an object.")
        try:
            payload = self.tools[name][0].model_validate(arguments)
        except ValidationError:
            return _error(422, "Invalid tool arguments. Follow the tool's parameter schema.")
        try:
            if name == "web_search":
                return await self._search(payload.query)
            if name == "open_page":
                return await self._open(payload.url)
            return await self._submit(payload, call_id)
        except WorldError as error:
            return _error(error.status_code, self._detail(error))
        except (psycopg.OperationalError, PoolTimeout):
            return _error(503, _UNAVAILABLE)

    @staticmethod
    def _detail(error: WorldError) -> str:
        # Run-level errors use internal words. The assistant only needs to know to try later.
        detail = str(error)
        return _NOT_RUNNING if detail in _INTERNAL_ERRORS else detail

    async def _open(self, url: str) -> dict:
        async with self.pool.connection() as conn:
            page = await sim_web.open_url(conn, self.run_id, self.owner_id, url)
        return {"ok": True, "data": _page_data(page)}

    async def _submit(self, payload: SubmitForm, call_id: str) -> dict:
        site_id, _, _ = sim_web.parse_url(payload.url)
        known = None
        async with self.pool.connection() as conn:
            if site_id is not None:
                known = await _one(
                    conn,
                    "SELECT 1 FROM web_sites WHERE run_id=%s AND site_id=%s",
                    (self.run_id, site_id),
                )
            if known is None:
                return {"ok": True, "data": _page_data(sim_web.not_found_page(payload.url))}
            result = await sim_web.submit_action(
                conn,
                self.run_id,
                self.owner_id,
                site_id,
                payload.action,
                payload.fields,
                payload.request_id or str(uuid5(self.operation_namespace, call_id)),
            )
        return {
            "ok": True,
            "data": {key: result[key] for key in ("url", "title", "text", "outcome")},
        }

    async def _search(self, query: str) -> dict:
        async with self.pool.connection() as conn:
            results = await sim_web.search_sites(conn, self.run_id, query)
        if not results and self.director_brain is not None:
            results = await self._ask_for_site(query)
        return {"ok": True, "data": {"text": _search_text(query, results)}}

    async def _ask_for_site(self, query: str) -> list[dict]:
        """Ask for one new site when no site fits. No connection is held during the request.

        A brain failure such as a refused budget propagates to the caller, so the engine can
        pause or retry the same tool call. The saved claim bounds how often it can ask.
        """
        async with self.pool.connection() as conn, conn.transaction():
            run = await _one(conn, "SELECT * FROM environment_runs WHERE id=%s", (self.run_id,))
            if run is None or run["status"] != "running":
                return []
            director = wallet.clock_section(run).get("director") or {}
            if director.get("enabled") is False:
                return []
            key = await sim_web.reserve_search_ask(conn, self.run_id, self.owner_id, query)
            now_s = await wallet.sim_now(conn, self.run_id)
        if key is None:
            return []
        try:
            plan = await self.director_brain.site_for_search(run, self.owner_id, query, now_s)
        except ModelGenerationError:
            async with self.pool.connection() as conn:
                await sim_web.release_search_ask(conn, self.run_id, key)
            raise
        if getattr(plan, "_fell_back", False):
            # No decision was made, so this is not an answer. The claim stays open, and a later
            # search can ask again up to the attempt limit.
            return []
        created = None
        async with self.pool.connection() as conn, conn.transaction():
            for spec in _proposed_sites(plan)[:1]:
                try:
                    created = await sim_web.create_site(
                        conn, self.run_id, spec, operation_key=f"search:{key}"
                    )
                except WorldError:
                    # The address is taken, the limit was reached or the spec is not valid.
                    created = None
            await sim_web.finish_search_ask(
                conn, self.run_id, key, created["site_id"] if created else None
            )
        if created is None:
            return []
        async with self.pool.connection() as conn:
            found = await sim_web.search_sites(conn, self.run_id, query)
            if any(result["site_id"] == created["site_id"] for result in found):
                return found
            site = await _one(
                conn,
                "SELECT site_id,name,kind,spec->>'tagline' AS tagline FROM web_sites "
                "WHERE run_id=%s AND site_id=%s",
                (self.run_id, created["site_id"]),
            )
        return [site, *found][: sim_web.SEARCH_LIMIT]
