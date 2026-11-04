"""The live world's searchable memory: PostgreSQL full-text search mixed with recency.

A trigger on run_events fills this log for clocked runs. Board events never enter it,
and direct writes of board kinds are refused, so nothing retrieved here can quote the
assistants' board. All functions run on the caller's connection and transaction.
"""

from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from agentrpg.world import _rows

TITLE_LIMIT = 300
BODY_LIMIT = 4000
QUERY_LIMIT = 2000
MAX_RESULTS = 100
# Relevance decides first. Recency moves a score by at most 30 percent.
RECENCY_WEIGHT = 0.3
RECENCY_HALF_LIFE_S = 2 * 24 * 3600
COLUMNS = "l.id,l.seq,l.run_id,l.sim_s,l.kind,l.owner_id,l.title,l.body,l.meta"


class DirectorLogError(ValueError):
    """A write that the log must refuse."""


async def log(conn, run_id, sim_s, kind, title, body="", *, owner_id=None, meta=None, key=None):
    """Save one entry. With a key, a repeated write of the same entry keeps the first copy."""
    if not isinstance(kind, str) or "board" in kind:
        raise DirectorLogError("Board activity is not part of this log.")
    entry_id = uuid5(UUID(str(run_id)), f"director_log:{key}") if key else uuid4()
    await conn.execute(
        """INSERT INTO director_log(id,run_id,sim_s,kind,owner_id,title,body,meta)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
        (
            entry_id,
            run_id,
            sim_s,
            kind,
            owner_id,
            (title or "")[:TITLE_LIMIT],
            (body or "")[:BODY_LIMIT],
            Jsonb(meta or {}),
        ),
    )
    return entry_id


def _filters(args, owner_id, kinds, since_s, until_s=None):
    clause = ""
    if owner_id is not None:
        clause += " AND l.owner_id=%(owner)s"
        args["owner"] = owner_id
    if kinds is not None:
        clause += " AND l.kind=ANY(%(kinds)s)"
        args["kinds"] = list(kinds)
    if since_s is not None:
        clause += " AND l.sim_s>=%(since)s"
        args["since"] = since_s
    if until_s is not None:
        clause += " AND l.sim_s<=%(until)s"
        args["until"] = until_s
    return clause


async def recent(conn, run_id, limit=20, *, owner_id=None, kinds=None, since_s=None, until_s=None):
    """Newest entries first."""
    args = {"run": run_id, "limit": max(1, min(int(limit), MAX_RESULTS))}
    clause = _filters(args, owner_id, kinds, since_s, until_s)
    return await _rows(
        conn,
        f"""SELECT {COLUMNS} FROM director_log l WHERE l.run_id=%(run)s{clause}
            ORDER BY l.sim_s DESC, l.seq DESC LIMIT %(limit)s""",
        args,
    )


async def search(conn, run_id, query, k=8, owner_id=None, kinds=None, since_s=None):
    """Best matches first. Any query word can match; entries with more of them rank higher."""
    if not isinstance(query, str) or not query.strip():
        return []
    args = {
        "run": run_id,
        "query": query.replace("\x00", " ")[:QUERY_LIMIT],
        "limit": max(1, min(int(k), MAX_RESULTS)),
        "half_life": RECENCY_HALF_LIFE_S,
        "weight": RECENCY_WEIGHT,
    }
    clause = _filters(args, owner_id, kinds, since_s)
    return await _rows(
        conn,
        f"""WITH q AS (
                SELECT to_tsquery('english', replace(
                    plainto_tsquery('english', %(query)s)::text, ' & ', ' | ')) AS terms
            ), newest AS (
                SELECT COALESCE(max(sim_s), 0) AS sim_s FROM director_log WHERE run_id=%(run)s
            ), matches AS (
                SELECT {COLUMNS}, ts_rank(l.tsv, q.terms, 32) AS text_rank,
                       power(0.5, GREATEST(newest.sim_s - l.sim_s, 0)::float8
                                  / %(half_life)s) AS recency
                FROM director_log l, q, newest
                WHERE l.run_id=%(run)s AND numnode(q.terms) > 0 AND l.tsv @@ q.terms{clause}
            )
            SELECT *, text_rank * ((1 - %(weight)s) + %(weight)s * recency) AS score
            FROM matches ORDER BY score DESC, sim_s DESC, seq DESC LIMIT %(limit)s""",
        args,
    )
