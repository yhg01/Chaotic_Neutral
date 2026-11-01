"""Owner workspaces, access checks, immutable audit records, and local search."""

import copy
import json
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from agentrpg.resource_schema import ResourceRequest, ResourceSpec
from agentrpg.world import WorldError, WorldService, _hash, _one, _rows

ResourceError = WorldError
RESOURCE_COLUMNS = """resource_id AS id, owner_id, origin_id, title, kind, content,
    quantity, visibility, shared_owner_ids, agent_writable, version"""
LISTING_COLUMNS = """listing_id::text AS id, seller_id, resource_id, origin_id, title,
    quantity, unit_price, version"""


class ResourceService:
    def __init__(self, pool):
        self.pool = pool
        self.world = WorldService(pool)

    async def initialize(self, run_id, specs, *, connection=None):
        if connection is None:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    return await self.initialize(run_id, specs, connection=conn)
        async with connection.transaction():
            await self.world._run(connection, run_id)
            try:
                items = [ResourceSpec.model_validate(s).model_dump(mode="json") for s in specs]
            except ValidationError as error:
                raise ResourceError("Invalid resource specification.", 422) from error
            owners = {
                r["owner_id"]
                for r in await _rows(
                    connection, "SELECT owner_id FROM run_participants WHERE run_id=%s", (run_id,)
                )
            }
            if len(items) > 150 or len({i["id"] for i in items}) != len(items):
                raise ResourceError("Resource IDs must be unique, with at most 150 resources.", 422)
            for item in items:
                if item["owner_id"] not in owners or set(item["shared_owner_ids"]) - owners:
                    raise ResourceError("A resource names an unknown owner.", 422)
            digest = _hash(items)
            old = await _one(connection, "SELECT * FROM resource_state WHERE run_id=%s", (run_id,))
            if old:
                if old["initialization_hash"] != digest:
                    raise ResourceError(
                        "Resources were initialized with a different specification."
                    )
                return await self._snapshot(connection, run_id)
            await connection.execute(
                "INSERT INTO resource_state(run_id,initialization_hash) VALUES(%s,%s)",
                (run_id, digest),
            )
            for item in items:
                await connection.execute(
                    """INSERT INTO owner_resources(run_id, resource_id, owner_id, origin_id,
                    title, kind, content, quantity, visibility, shared_owner_ids,
                    agent_writable) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run_id,
                        item["id"],
                        item["owner_id"],
                        item["id"],
                        item["title"],
                        item["kind"],
                        item["content"],
                        item["quantity"],
                        item["visibility"],
                        item["shared_owner_ids"],
                        item["agent_writable"],
                    ),
                )
            await connection.execute(
                """INSERT INTO market_permissions(run_id, owner_id) SELECT run_id, owner_id FROM
                world_accounts WHERE run_id=%s""",
                (run_id,),
            )
            state = await self._snapshot(connection, run_id)
            await connection.execute(
                (
                    """INSERT INTO resource_journal(run_id, revision, kind, payload) VALUES(%s, 0,
                    'initialized', %s)"""
                ),
                (run_id, Jsonb(state)),
            )
            return state

    async def _snapshot(self, conn, run_id):
        state = await _one(conn, "SELECT revision FROM resource_state WHERE run_id=%s", (run_id,))
        if state is None:
            raise ResourceError("Resources not found.", 404)
        return {
            **state,
            "resources": await _rows(
                conn,
                f"SELECT {RESOURCE_COLUMNS} FROM owner_resources "
                "WHERE run_id=%s ORDER BY resource_id",
                (run_id,),
            ),
            "permissions": await _rows(
                conn,
                """SELECT owner_id, buy_budget, buy_items, sell_limits, version, expires_window
                FROM market_permissions WHERE run_id=%s ORDER BY owner_id""",
                (run_id,),
            ),
            "listings": await _rows(
                conn,
                f"SELECT {LISTING_COLUMNS},consent_version FROM market_listings "
                "WHERE run_id=%s ORDER BY listing_id",
                (run_id,),
            ),
            "ledger": await _rows(
                conn,
                """SELECT owner_id, request_id::text, seller_id, listing_id::text, resource_id,
                quantity, amount, buyer_balance, seller_balance, window_index FROM market_ledger
                WHERE run_id=%s ORDER BY window_index, owner_id, request_id""",
                (run_id,),
            ),
        }

    async def snapshot(self, run_id):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await self.world._run(conn, run_id)
                return await self._snapshot(conn, run_id)

    async def _journal(self, conn, run_id, kind):
        await conn.execute(
            "UPDATE resource_state SET revision=revision+1 WHERE run_id=%s", (run_id,)
        )
        state = await self._snapshot(conn, run_id)
        await conn.execute(
            "INSERT INTO resource_journal(run_id,revision,kind,payload) VALUES(%s,%s,%s,%s)",
            (run_id, state["revision"], kind, Jsonb(state)),
        )

    async def journal(self, run_id):
        async with self.pool.connection() as conn:
            records = await _rows(
                conn,
                (
                    """SELECT revision, kind, payload FROM resource_journal WHERE run_id=%s ORDER
                    BY revision"""
                ),
                (run_id,),
            )
            if not records:
                raise ResourceError("Resources not found.", 404)
            return records

    @staticmethod
    def replay(records):
        if not records or records[0]["revision"] != 0 or records[0]["kind"] != "initialized":
            raise ResourceError("Replay must start with initial resources.", 422)
        for index, record in enumerate(records):
            if record["revision"] != index or record["payload"]["revision"] != index:
                raise ResourceError("Replay contains a missing or repeated revision.", 422)
        return copy.deepcopy(records[-1]["payload"])

    @staticmethod
    def _visible(resource, owner_id):
        return (
            resource["owner_id"] == owner_id
            or resource["visibility"] == "public"
            or owner_id in resource["shared_owner_ids"]
        )

    async def _visible_resources(self, run_id, owner_id, *, connection=None):
        if connection is None:
            async with self.pool.connection() as conn:
                return await self._visible_resources(run_id, owner_id, connection=conn)
        async with connection.transaction():
            await self.world._run(connection, run_id)
            await self.world._account(connection, run_id, owner_id)
            rows = await _rows(
                connection,
                f"SELECT {RESOURCE_COLUMNS} FROM owner_resources "
                "WHERE run_id=%s ORDER BY resource_id",
                (run_id,),
            )
            return [r for r in rows if self._visible(r, owner_id)]

    async def view(self, run_id, owner_id, *, connection=None):
        """List accessible metadata. Fetch returns one complete resource body."""
        rows = await self._visible_resources(run_id, owner_id, connection=connection)
        return {
            "owner_id": owner_id,
            "items": [{k: v for k, v in r.items() if k != "content"} for r in rows[:100]],
            "truncated": len(rows) > 100,
        }

    async def actor_view(self, run_id, owner_id, *, connection=None):
        rows = await self._visible_resources(run_id, owner_id, connection=connection)
        own = [r for r in rows if r["owner_id"] == owner_id]
        known_items = [r["id"] for r in rows if r["kind"] == "item"]
        result = {
            "owner_id": owner_id,
            "items": [],
            "truncated": False,
            "visible_item_ids": known_items[:100],
            "visible_items_truncated": len(known_items) > 100,
        }
        bodies = []
        for resource in own[:30]:
            summary = {**resource, "content": "", "content_truncated": True}
            if len(json.dumps({**result, "items": [*result["items"], summary]})) > 23000:
                break
            result["items"].append(summary)
            bodies.append(resource["content"])
        result["truncated"] = len(result["items"]) < len(own)
        remaining = 24000 - len(json.dumps(result)) - len(bodies)
        for index, (item, body) in enumerate(zip(result["items"], bodies, strict=True)):
            allowance = max(0, remaining // (len(bodies) - index))
            low, high = 0, min(4000, len(body))
            while low < high:
                midpoint = (low + high + 1) // 2
                if len(json.dumps(body[:midpoint])) - 2 <= allowance:
                    low = midpoint
                else:
                    high = midpoint - 1
            item.update(content=body[:low], content_truncated=low < len(body))
            remaining -= len(json.dumps(body[:low])) - 2
        return result

    async def search(self, run_id, owner_id, query=""):
        if not isinstance(query, str) or len(query) > 200:
            raise ResourceError("Search text must have at most 200 characters.", 422)
        resources = await self._visible_resources(run_id, owner_id)
        query = query.casefold().strip()
        items = [
            {
                "type": "resource",
                **{k: v for k, v in r.items() if k not in {"content", "shared_owner_ids"}},
            }
            for r in resources
            if query in (r["title"] + " " + r["content"] + " " + r["id"]).casefold()
        ]
        async with self.pool.connection() as conn:
            listings = await self._listings(conn, run_id)
        items.extend(
            {"type": "listing", **r}
            for r in listings
            if query in (r["title"] + " " + r["resource_id"] + " " + r["id"]).casefold()
        )
        return {"items": items[:100], "truncated": len(items) > 100}

    async def _listings(self, conn, run_id):
        # Revoked and expired listings are not advertised. Old receipts stay readable.
        return await _rows(
            conn,
            f"""SELECT {", ".join("l." + part.strip() for part in LISTING_COLUMNS.split(","))}
            FROM market_listings l JOIN market_permissions p
            ON p.run_id=l.run_id AND p.owner_id=l.seller_id
            JOIN environment_runs r ON r.id=l.run_id
            WHERE l.run_id=%s AND l.quantity>0 AND l.consent_version=p.version
              AND (p.expires_window IS NULL OR p.expires_window>=r.current_window)
            ORDER BY l.listing_id""",
            (run_id,),
        )

    async def fetch(self, run_id, owner_id, resource_id):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await self.world._run(conn, run_id)
                await self.world._account(conn, run_id, owner_id)
                resource = await _one(
                    conn,
                    f"SELECT {RESOURCE_COLUMNS} FROM owner_resources "
                    "WHERE run_id=%s AND resource_id=%s",
                    (run_id, resource_id),
                )
                if resource and self._visible(resource, owner_id):
                    return {"type": "resource", **resource}
                for listing in await self._listings(conn, run_id):
                    if listing["id"] == str(resource_id):
                        return {"type": "listing", **listing}
                raise ResourceError("Resource not found.", 404)

    async def update(
        self, run_id, owner_id, resource_id, request_id, content, expected_version, *, title=None
    ):
        return await self._write(
            run_id, owner_id, resource_id, request_id, content, expected_version, title=title
        )

    async def director_update(
        self,
        run_id,
        owner_id,
        resource_id,
        request_id,
        content,
        expected_version,
        *,
        title=None,
        connection=None,
    ):
        return await self._write(
            run_id,
            owner_id,
            resource_id,
            request_id,
            content,
            expected_version,
            title=title,
            trusted=True,
            connection=connection,
        )

    async def _write(
        self,
        run_id,
        owner_id,
        resource_id,
        request_id,
        content,
        expected_version,
        *,
        title=None,
        trusted=False,
        connection=None,
    ):
        if (
            not isinstance(content, str)
            or len(content) > 12000
            or type(expected_version) is not int
            or expected_version < 0
        ):
            raise ResourceError("Content and expected resource version are invalid.", 422)
        if title is not None and (
            not isinstance(title, str) or not title.strip() or len(title) > 200
        ):
            raise ResourceError("Resource title is invalid.", 422)
        if connection is None:
            async with self.pool.connection() as conn:
                return await self._write(
                    run_id,
                    owner_id,
                    resource_id,
                    request_id,
                    content,
                    expected_version,
                    title=title,
                    trusted=trusted,
                    connection=conn,
                )
        async with connection.transaction():
            run = await self.world._run(connection, run_id)
            await self.world._account(connection, run_id, owner_id)
            payload = {
                "resource_id": resource_id,
                "content": content,
                "expected_version": expected_version,
                "title": title,
                "trusted": trusted,
            }
            old = await _one(
                connection,
                "SELECT * FROM resource_requests WHERE run_id=%s AND owner_id=%s AND request_id=%s",
                (run_id, owner_id, request_id),
            )
            if old:
                if old["payload_hash"] != _hash(payload):
                    raise ResourceError("The request ID was used with a different payload.")
                return old["result"]
            self.world._active(run)
            resource = await _one(
                connection,
                f"SELECT {RESOURCE_COLUMNS} FROM owner_resources "
                "WHERE run_id=%s AND resource_id=%s",
                (run_id, resource_id),
            )
            if not resource or resource["owner_id"] != owner_id:
                raise ResourceError("Resource not found.", 404)
            if not trusted and not resource["agent_writable"]:
                raise ResourceError("This resource is read-only for the Agent.", 403)
            if resource["version"] != expected_version:
                raise ResourceError("The resource version has changed.")
            await connection.execute(
                """UPDATE owner_resources SET content=%s, title=%s, version=version+1 WHERE
                run_id=%s AND resource_id=%s""",
                (content, title or resource["title"], run_id, resource_id),
            )
            result = {
                **resource,
                "content": content,
                "title": title or resource["title"],
                "version": expected_version + 1,
            }
            await connection.execute(
                (
                    """INSERT INTO resource_requests(run_id, owner_id, request_id, payload_hash,
                    request_payload, result) VALUES(%s, %s, %s, %s, %s, %s)"""
                ),
                (run_id, owner_id, request_id, _hash(payload), Jsonb(payload), Jsonb(result)),
            )
            await self._journal(connection, run_id, "resource_updated")
            recipients = (
                [None]
                if result["visibility"] == "public"
                else [owner_id, *result["shared_owner_ids"]]
            )
            for recipient in recipients:
                await self.world._event(
                    connection,
                    run_id,
                    run["current_window"],
                    "resource_updated",
                    recipient,
                    f"resource:{owner_id}:{request_id}:{recipient}",
                    {"resource_id": resource_id, "version": result["version"]},
                )
            return result

    async def create_document(self, run_id, owner_id, request_id, title, content):

        operation = {"kind": "create", "title": title, "content": content}
        return await self.apply_request(run_id, owner_id, request_id, operation, trusted=False)

    async def apply_request(
        self, run_id, owner_id, request_id, operation, *, connection=None, trusted=True
    ):
        """Apply a typed Actor request within its job transaction, or create an Agent document."""

        try:
            operation = ResourceRequest.model_validate(operation)
        except ValidationError as error:
            raise ResourceError("Invalid resource request.", 422) from error
        if operation.kind == "update":
            return await self._write(
                run_id,
                owner_id,
                operation.resource_id,
                request_id,
                operation.content,
                operation.expected_version,
                title=operation.title,
                trusted=trusted,
                connection=connection,
            )
        if not trusted and operation.kind != "create":
            raise ResourceError("Only the owner can change resource access.", 403)
        if connection is None:
            async with self.pool.connection() as conn:
                return await self.apply_request(
                    run_id, owner_id, request_id, operation, connection=conn, trusted=trusted
                )
        async with connection.transaction():
            run = await self.world._run(connection, run_id)
            await self.world._account(connection, run_id, owner_id)
            payload = {"operation": operation.model_dump(mode="json"), "trusted": trusted}
            old = await _one(
                connection,
                "SELECT * FROM resource_requests WHERE run_id=%s AND owner_id=%s AND request_id=%s",
                (run_id, owner_id, request_id),
            )
            if old:
                if old["payload_hash"] != _hash(payload):
                    raise ResourceError("The request ID was used with a different payload.")
                return old["result"]
            self.world._active(run)
            if operation.kind == "create":
                count = await _one(
                    connection,
                    "SELECT count(*) AS count FROM owner_resources WHERE run_id=%s AND owner_id=%s",
                    (run_id, owner_id),
                )
                if count["count"] >= 100:
                    raise ResourceError("An owner can have at most 100 resources.")
                resource_id = str(uuid5(UUID(str(run_id)), f"document:{owner_id}:{request_id}"))
                await connection.execute(
                    (
                        """INSERT INTO owner_resources(run_id, resource_id, owner_id, origin_id,
                        title, kind, content) VALUES(%s, %s, %s, %s, %s, 'document', %s)"""
                    ),
                    (
                        run_id,
                        resource_id,
                        owner_id,
                        resource_id,
                        operation.title,
                        operation.content,
                    ),
                )
            else:
                resource_id = operation.resource_id
                resource = await _one(
                    connection,
                    (
                        """SELECT * FROM owner_resources WHERE run_id=%s AND resource_id=%s AND
                        owner_id=%s"""
                    ),
                    (run_id, resource_id, owner_id),
                )
                if not resource:
                    raise ResourceError("Resource not found.", 404)
                if resource["version"] != operation.expected_version:
                    raise ResourceError("The resource version has changed.")
                owners = {
                    r["owner_id"]
                    for r in await _rows(
                        connection,
                        "SELECT owner_id FROM run_participants WHERE run_id=%s",
                        (run_id,),
                    )
                }
                if (
                    set(operation.shared_owner_ids) - owners
                    or owner_id in operation.shared_owner_ids
                ):
                    raise ResourceError("Resource sharing requires other owners in this run.", 422)
                await connection.execute(
                    (
                        """UPDATE owner_resources SET visibility=%s, shared_owner_ids=%s,
                        version=version+1 WHERE run_id=%s AND resource_id=%s"""
                    ),
                    (operation.visibility, list(operation.shared_owner_ids), run_id, resource_id),
                )
            result = await _one(
                connection,
                f"SELECT {RESOURCE_COLUMNS} FROM owner_resources "
                "WHERE run_id=%s AND resource_id=%s",
                (run_id, resource_id),
            )
            await connection.execute(
                (
                    """INSERT INTO resource_requests(run_id, owner_id, request_id, payload_hash,
                    request_payload, result) VALUES(%s, %s, %s, %s, %s, %s)"""
                ),
                (run_id, owner_id, request_id, _hash(payload), Jsonb(payload), Jsonb(result)),
            )
            await self._journal(connection, run_id, "resource_" + operation.kind)
            recipients = (
                [None]
                if result["visibility"] == "public"
                else [owner_id, *result["shared_owner_ids"]]
            )
            for recipient in recipients:
                await self.world._event(
                    connection,
                    run_id,
                    run["current_window"],
                    "resource_updated",
                    recipient,
                    f"resource:{owner_id}:{request_id}:{recipient}",
                    {
                        "resource_id": resource_id,
                        "version": result["version"],
                        "kind": operation.kind,
                    },
                )
            return result
