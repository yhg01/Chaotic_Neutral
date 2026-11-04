"""Read-only views of the invented sites for the human observer and the replay portal.

These routes use the same operator check as /runs and /console. A token-free request must
be a direct, same-origin loopback request. An Agent credential is refused. No route here
changes a record, and the HTML page carries no script and no working form.
"""

import html
from typing import Annotated
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse

from agentrpg import sim_web, wallet
from agentrpg.director import row, rows
from agentrpg.main import OPERATOR_AUTH, require_admin

router = APIRouter(tags=["simulated web"], include_in_schema=False)
SiteID = Annotated[str, Path(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
Owner = Annotated[str | None, Query(max_length=80)]
ROW_LIMIT = 1000
_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}


async def _run_or_404(conn, run_id):
    run = await row(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,))
    if run is None:
        raise HTTPException(404, "Run not found.")
    return run


def _href(run_id, site_id, owner_id, path="/", query=None):
    base = f"/sim/{run_id}/{site_id}{'' if path == '/' else path}"
    if path == "/":
        base += "/"
    args = {**(query or {}), **({"owner": owner_id} if owner_id else {})}
    return base + (f"?{urlencode(args)}" if args else "")


def _local_link(run_id, site_id, owner_id):
    def link(url: str) -> str | None:
        target, path, query = sim_web.parse_url(url)
        return _href(run_id, site_id, owner_id, path, query) if target == site_id else None

    return link


def _banner(run_id, site_id, owners, owner_id, path) -> list[str]:
    """Return finished HTML. Each value is escaped here, because render_html adds it as is."""

    def e(value) -> str:
        return html.escape(str(value), quote=True)

    people = [
        (
            f"<strong>{e(person)}</strong>"
            if person == owner_id
            else f'<a href="{e(_href(run_id, site_id, person, path))}">{e(person)}</a>'
        )
        for person in owners
    ]
    return [
        f'<a href="/sim/{e(run_id)}">All sites</a>',
        "Read-only view. See the page as:",
        " ".join(people) or "no people in this record",
    ]


@router.get("/sim/{run_id}", response_class=HTMLResponse)
async def site_index(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    require_admin(principal)
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
        sites = await rows(
            conn,
            "SELECT site_id,name,kind,version,spec->>'tagline' AS tagline FROM web_sites "
            "WHERE run_id=%s ORDER BY sim_s,site_id",
            (run_id,),
        )
    page = sim_web.Page(
        url=f"/sim/{run_id}",
        title="Sites in this record",
        sections=[
            sim_web.Section(
                lines=[] if sites else ["No sites were created in this record."],
                links=[
                    sim_web.Link(
                        f"{site['name']} ({site['kind']}, version {site['version']})",
                        sim_web.site_url(site["site_id"]),
                    )
                    for site in sites
                ],
            )
        ],
    )

    def link(url: str) -> str | None:
        target, _, _ = sim_web.parse_url(url)
        return _href(run_id, target, None) if target else None

    return HTMLResponse(sim_web.render_html(page, link=link), headers=_HEADERS)


@router.get("/sim/{run_id}/{site_id}", response_class=HTMLResponse)
@router.get("/sim/{run_id}/{site_id}/", response_class=HTMLResponse)
@router.get("/sim/{run_id}/{site_id}/{path:path}", response_class=HTMLResponse)
async def site_page(
    run_id: UUID,
    site_id: SiteID,
    request: Request,
    principal: OPERATOR_AUTH,
    path: str = "",
    owner: Owner = None,
):
    require_admin(principal)
    query = {key: value for key, value in request.query_params.items() if key in {"page", "q"}}
    async with request.app.state.pool.connection() as conn:
        await _run_or_404(conn, run_id)
        owners = [
            item["owner_id"]
            for item in await rows(
                conn,
                "SELECT owner_id FROM run_participants WHERE run_id=%s ORDER BY owner_id",
                (run_id,),
            )
        ]
        if owner is not None and owner not in owners:
            raise HTTPException(404, "This person is not in the record.")
        # urlsplit drops any scheme or host text in the captured path.
        clean = "/" + "/".join(part for part in urlsplit(path).path.split("/") if part)
        page = await sim_web.build_page(conn, run_id, site_id, clean, query, owner)
    if page is None:
        raise HTTPException(404, "Site not found.")
    body = sim_web.render_html(
        page,
        link=_local_link(run_id, site_id, owner),
        banner=_banner(run_id, site_id, owners, owner, clean),
    )
    return HTMLResponse(body, status_code=page.status, headers=_HEADERS)


def _site_rows(records, site_id):
    return [record for record in records if record["site_id"] == site_id]


@router.get("/api/v1/observer/runs/{run_id}/web", include_in_schema=False)
async def web_records(run_id: UUID, request: Request, principal: OPERATOR_AUTH):
    """Sites of one run with their orders, listings, bills and declined payments."""
    require_admin(principal)
    limit = (run_id, ROW_LIMIT)
    async with request.app.state.pool.connection() as conn:
        run = await _run_or_404(conn, run_id)
        sites = await rows(
            conn,
            "SELECT site_id,name,kind,version,source,sim_s,spec->>'tagline' AS tagline "
            "FROM web_sites WHERE run_id=%s ORDER BY sim_s,site_id",
            (run_id,),
        )
        orders = await rows(
            conn,
            "SELECT order_id,owner_id,site_id,lines,subtotal,delivery_fee,total,status,eta_s,"
            "sim_s FROM web_orders WHERE run_id=%s ORDER BY sim_s,order_id LIMIT %s",
            limit,
        )
        listings = await rows(
            conn,
            "SELECT listing_id,owner_id,site_id,title,description,category,price,status,version,"
            "sim_s FROM web_listings WHERE run_id=%s ORDER BY sim_s,listing_id LIMIT %s",
            limit,
        )
        bills = await rows(
            conn,
            "SELECT bill_id,owner_id,site_id,title,amount,due_text,status,paid_sim_s "
            "FROM web_bills WHERE run_id=%s ORDER BY site_id,bill_id LIMIT %s",
            limit,
        )
        bookings = await rows(
            conn,
            "SELECT booking_id,owner_id,site_id,slot_id,title,price,status,sim_s "
            "FROM web_bookings WHERE run_id=%s ORDER BY sim_s,booking_id LIMIT %s",
            limit,
        )
        applications = await rows(
            conn,
            "SELECT application_id,owner_id,site_id,posting_id,message,status,sim_s "
            "FROM web_applications WHERE run_id=%s ORDER BY sim_s,application_id LIMIT %s",
            limit,
        )
        events = await rows(
            conn,
            """SELECT id,kind,owner_id,payload,created_at FROM run_events
               WHERE run_id=%s AND kind IN ('web_spend_declined','site_message')
               ORDER BY created_at,id LIMIT %s""",
            limit,
        )
        accounts = await rows(
            conn, "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s", (run_id,)
        )
        wallets = [
            {
                "owner_id": account["owner_id"],
                "balance": account["balance"],
                **await wallet.limits(conn, run_id, account["owner_id"], run=run),
            }
            for account in sorted(accounts, key=lambda item: item["owner_id"])
        ]
    declines = [event for event in events if event["kind"] == "web_spend_declined"]
    messages = [event for event in events if event["kind"] == "site_message"]

    def from_site(records, site_id):
        return [item for item in records if item["payload"].get("site_id") == site_id]

    return {
        "wallets": wallets,
        "sites": [
            {
                **site,
                "url": sim_web.site_url(site["site_id"]),
                "observer_url": _href(run_id, site["site_id"], None),
                "orders": _site_rows(orders, site["site_id"]),
                "listings": _site_rows(listings, site["site_id"]),
                "bills": _site_rows(bills, site["site_id"]),
                "bookings": _site_rows(bookings, site["site_id"]),
                "applications": _site_rows(applications, site["site_id"]),
                "spend_declines": from_site(declines, site["site_id"]),
                "messages": from_site(messages, site["site_id"]),
            }
            for site in sites
        ],
    }
