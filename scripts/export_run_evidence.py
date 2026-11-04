"""Export one clocked run's saved records for reading. Read-only; makes no model calls.

Usage: scripts/export_run_evidence.py RUN_ID [--out DIR]
Writes JSON Lines per record type and one timeline.md in story order under .local/run-evidence.
"""

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from agentrpg.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SHORT = 1500


def clean(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def clip(text, limit=SHORT):
    text = text if isinstance(text, str) else json.dumps(clean(text), ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars]"


def story(value):
    return value.strftime("%a %H:%M") if hasattr(value, "strftime") else str(value)


async def fetch(conn, query, args=()):
    return await (await conn.execute(query, args)).fetchall()


async def export(run_id: UUID, out: Path) -> dict:
    url = Settings().database_url.get_secret_value()
    async with await AsyncConnection.connect(url, row_factory=dict_row) as conn:
        run = (await fetch(conn, "SELECT * FROM environment_runs WHERE id=%s", (run_id,)))[0]
        people = await fetch(conn, "SELECT * FROM run_participants WHERE run_id=%s", (run_id,))
        by_agent = {str(p["agent_id"]): p["owner_id"] for p in people}
        state = await fetch(conn, "SELECT * FROM clock_state WHERE run_id=%s", (run_id,))
        board = await fetch(
            conn,
            "SELECT id,author_id,parent_id,thread_id,title,body,audience,created_at,seq "
            "FROM messages WHERE cluster_id=%s ORDER BY seq",
            (run["cluster_id"],),
        )
        roots = {str(m["id"]): m for m in board if m["parent_id"] is None}
        for m in board:
            root = roots.get(str(m["thread_id"]))
            m["author"] = by_agent.get(str(m["author_id"]), str(m["author_id"]))
            audience = root["audience"] if root else None
            m["visibility"] = "direct" if audience else "public"
            m["participants"] = (
                sorted(
                    {by_agent.get(str(a), str(a)) for a in audience}
                    | {by_agent.get(str(root["author_id"]))}
                )
                if audience
                else None
            )
        chat = await fetch(
            conn,
            "SELECT owner_id,source,content,created_at FROM owner_messages WHERE run_id=%s "
            "ORDER BY created_at,id",
            (run_id,),
        )
        mail = await fetch(
            conn,
            "SELECT sender_owner_id,sender_source,recipient_owner_id,subject,body,created_at "
            "FROM inbox_messages WHERE run_id=%s ORDER BY created_at,id",
            (run_id,),
        )
        events = await fetch(
            conn,
            'SELECT kind,owner_id,payload,created_at,"window",cause_process FROM run_events '
            "WHERE run_id=%s ORDER BY created_at,id",
            (run_id,),
        )
        actions = await fetch(
            conn,
            "SELECT owner_id,process,label,detail,start_s,duration_s,status,reachable "
            "FROM clock_actions WHERE run_id=%s ORDER BY start_s,key",
            (run_id,),
        )
        draws = await fetch(
            conn, "SELECT * FROM clock_draws WHERE run_id=%s ORDER BY fire_s NULLS LAST", (run_id,)
        )
        processes = await fetch(conn, "SELECT * FROM clock_processes WHERE run_id=%s", (run_id,))
        jobs = await fetch(
            conn,
            "SELECT owner_id,kind,status,clock_seq,attempts,start_s,end_s,failure_kind,output "
            "FROM run_jobs WHERE run_id=%s ORDER BY start_s,owner_id,kind",
            (run_id,),
        )
        ledger = await fetch(
            conn,
            "SELECT owner_id,delta,balance_after,memo,source,spend,sim_s FROM wallet_ledger "
            "WHERE run_id=%s ORDER BY sim_s,created_at",
            (run_id,),
        )
        approvals = await fetch(
            conn, "SELECT owner_id,amount,sim_s FROM wallet_approvals WHERE run_id=%s", (run_id,)
        )
        sites = await fetch(
            conn,
            "SELECT site_id,name,kind,source,sim_s,spec FROM web_sites WHERE run_id=%s "
            "ORDER BY sim_s,site_id",
            (run_id,),
        )
        orders = await fetch(
            conn, "SELECT * FROM web_orders WHERE run_id=%s ORDER BY sim_s,order_id", (run_id,)
        )
        listings = await fetch(
            conn, "SELECT * FROM web_listings WHERE run_id=%s ORDER BY sim_s,listing_id", (run_id,)
        )
        balances = await fetch(
            conn, "SELECT owner_id,balance FROM world_accounts WHERE run_id=%s", (run_id,)
        )
        spend = await fetch(
            conn,
            "SELECT role,count(*) AS attempts,sum(reported_usd) AS usd "
            "FROM model_spend_attempts WHERE run_id=%s GROUP BY role ORDER BY role",
            (run_id,),
        )

    def dump(name, items):
        with (out / f"{name}.jsonl").open("w") as handle:
            for item in items:
                handle.write(json.dumps(clean(item), ensure_ascii=False) + "\n")

    dump("board", board)
    dump("owner_chat", chat)
    dump("mail", mail)
    dump("actions", actions)
    dump("draws", draws)
    dump("processes", processes)
    dump("jobs", jobs)
    dump("ledger", ledger)
    dump("approvals", approvals)
    dump("sites", sites)
    dump("orders", orders)
    dump("listings", listings)
    dump("events", [e for e in events if e["kind"] not in {"assistant_tool_call"}])
    tools = [
        {
            "time": e["created_at"],
            "owner": e["owner_id"],
            "tool": e["payload"].get("tool"),
            "arguments": e["payload"].get("arguments"),
            "result": clip(e["payload"].get("result"), 4000),
        }
        for e in events
        if e["kind"] == "assistant_tool_call"
    ]
    dump("tools", tools)

    lines = [f"# Run {run_id}", ""]
    timeline = []
    for m in board:
        where = "DM " + "+".join(m["participants"]) if m["visibility"] == "direct" else "BOARD"
        title = f" [{m['title']}]" if m["title"] else ""
        timeline.append((m["created_at"], f"{where} {m['author']}{title}: {clip(m['body'])}"))
    for m in chat:
        who = "OWNER" if m["source"] != "agent" else "ASSISTANT"
        timeline.append((m["created_at"], f"CHAT {m['owner_id']} {who}: {clip(m['content'])}"))
    for m in mail:
        frm = m["sender_owner_id"] or m["sender_source"]
        timeline.append(
            (
                m["created_at"],
                f"MAIL {frm} to {m['recipient_owner_id']} [{m['subject']}]: {clip(m['body'], 700)}",
            )
        )
    for t in tools:
        if t["tool"] in {"read_owner_messages", "get_updates", "acknowledge", "read_run_view"}:
            continue
        timeline.append(
            (
                t["time"],
                f"TOOL {t['owner']} {t['tool']} {clip(t['arguments'], 400)} -> "
                f"{clip(t['result'], 300)}",
            )
        )
    interesting = {
        "assistant_expired",
        "action_replaced",
        "activation_abandoned",
        "director_fallback",
        "director_plan_applied",
        "web_spend_declined",
        "site_message",
        "simulator_error",
        "run_paused",
        "run_completed",
    }
    for e in events:
        if e["kind"] in interesting:
            timeline.append(
                (
                    e["created_at"],
                    f"EVENT {e['kind']} {e['owner_id'] or ''}: {clip(e['payload'], 700)}",
                )
            )
    timeline.sort(key=lambda item: item[0])
    lines += [f"- {story(when)} {text}" for when, text in timeline]
    (out / "timeline.md").write_text("\n".join(lines) + "\n")

    scenario = run["scenario"]
    summary = {
        "run_id": str(run_id),
        "scenario": scenario.get("id"),
        "drama": (scenario.get("clock") or {}).get("drama"),
        "status": run["status"],
        "result": run["result"],
        "sim_s": state[0]["sim_s"] if state else None,
        "balances": {b["owner_id"]: b["balance"] for b in balances},
        "spend": spend,
        "counts": {
            "board_public": sum(1 for m in board if m["visibility"] == "public"),
            "board_direct": sum(1 for m in board if m["visibility"] == "direct"),
            "owner_chat": len(chat),
            "mail": len(mail),
            "tool_calls": len(tools),
            "agent_jobs": sum(1 for j in jobs if j["kind"] == "agent"),
            "actor_jobs": sum(1 for j in jobs if j["kind"] == "actor"),
            "actions": len(actions),
            "draws": len(draws),
            "interventions": sum(1 for d in draws if d["outcome"] != "none"),
            "extreme": sum(1 for d in draws if d["outcome"] == "extreme"),
            "sites": len(sites),
            "orders": len(orders),
            "listings": len(listings),
        },
        "processes": [
            {k: p[k] for k in ("owner_id", "kind", "state", "bar_s", "activation_seq")}
            for p in processes
        ],
    }
    (out / "summary.json").write_text(json.dumps(clean(summary), indent=1, ensure_ascii=False))
    return clean(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or ROOT / ".local/run-evidence" / str(args.run_id)
    out.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(export(args.run_id, out)), indent=1, ensure_ascii=False))
    print(out)


if __name__ == "__main__":
    main()
