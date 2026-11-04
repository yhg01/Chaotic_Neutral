"""Durable purchase tasks, carts and orders over the existing simulated market."""

import json
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from agentrpg.director import emit, fingerprint, row, rows
from agentrpg.market import MarketService
from agentrpg.world import WorldError, WorldService


def plain(value):
    return json.loads(json.dumps(value, default=str))


class ShopService:
    def __init__(self, pool):
        self.pool = pool
        self.world = WorldService(pool)
        self.market = MarketService(pool)

    @asynccontextmanager
    async def connection(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.pool.connection() as conn:
                yield conn

    @staticmethod
    async def enabled(conn, run_id):
        return bool(await row(conn, "SELECT 1 FROM shop_profiles WHERE run_id=%s", (run_id,)))

    async def enable(self, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            if await self.enabled(conn, run_id):
                return
            if run["status"] != "pending":
                raise WorldError("Enable the browser shop before starting a new run.")
            await conn.execute("INSERT INTO shop_profiles VALUES (%s)", (run_id,))
            await conn.execute(
                "INSERT INTO shop_carts(run_id,owner_id) "
                "SELECT run_id,owner_id FROM world_accounts "
                "WHERE run_id=%s",
                (run_id,),
            )

    async def _require(self, conn, run_id, owner_id):
        if not await self.enabled(conn, run_id):
            raise WorldError("The browser shop is not enabled for this run.", 404)
        await self.world._account(conn, run_id, owner_id)

    async def _prior(self, conn, run_id, owner_id, request_id, payload, action_hash=None):
        prior = await row(
            conn,
            "SELECT * FROM shop_operations WHERE run_id=%s AND owner_id=%s AND request_id=%s",
            (run_id, owner_id, request_id),
        )
        if prior and (
            prior["payload_hash"] != fingerprint(plain(payload))
            or prior["action_hash"] != action_hash
        ):
            raise WorldError("The shopping operation ID has different arguments.")
        return prior["result"] if prior else None

    async def _save(self, conn, run_id, owner_id, request_id, payload, result, action_hash=None):
        result, payload = plain(result), plain(payload)
        await conn.execute(
            "INSERT INTO shop_operations VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                owner_id,
                request_id,
                Jsonb(payload),
                fingerprint(payload),
                action_hash,
                Jsonb(result),
            ),
        )
        return result

    async def tasks(self, run_id, owner_id, *, connection=None):
        async with self.connection(connection) as conn:
            return await rows(
                conn,
                "SELECT * FROM shop_tasks WHERE run_id=%s AND owner_id=%s "
                "ORDER BY (status IN ('open','submitted')) DESC,created_at DESC,id LIMIT 100",
                (run_id, owner_id),
            )

    async def owner_request(
        self, run_id, owner_id, message_id, request_id, command, *, connection=None
    ):
        payload = {
            "kind": "owner_task",
            "message_id": str(message_id),
            **command.model_dump(mode="json"),
        }
        async with self.connection(connection) as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            await self._require(conn, run_id, owner_id)
            prior = await self._prior(conn, run_id, owner_id, request_id, payload)
            if prior is not None:
                return prior
            self.world._active(run)
            source = await row(
                conn,
                "SELECT 1 FROM owner_messages WHERE id=%s AND run_id=%s "
                "AND owner_id=%s AND source='owner'",
                (message_id, run_id, owner_id),
            )
            if not source:
                raise WorldError("A shopping task requires its own owner message.", 403)
            existing = await row(
                conn,
                "SELECT * FROM shop_tasks WHERE run_id=%s AND owner_id=%s AND task_key=%s",
                (run_id, owner_id, command.task_key),
            )
            if command.kind == "cancel":
                if not existing or existing["status"] != "open":
                    raise WorldError("Only an open, unsubmitted shopping task can be cancelled.")
                await conn.execute(
                    "UPDATE shop_tasks SET status='cancelled' WHERE id=%s", (existing["id"],)
                )
                task_id = existing["id"]
            else:
                if existing:
                    raise WorldError("This shopping task key already exists.")
                current = await self.tasks(run_id, owner_id, connection=conn)
                if sum(t["status"] in {"open", "submitted"} for t in current) >= 20:
                    raise WorldError("This owner already has 20 open shopping tasks.")
                owner = next(o for o in run["scenario"]["owners"] if o["id"] == owner_id)
                if command.max_total > owner["max_spend"]:
                    raise WorldError("The shopping task exceeds the owner spending limit.", 403)
                known = await rows(
                    conn,
                    "SELECT resource_id AS id FROM owner_resources WHERE run_id=%s "
                    "AND kind='item' AND visibility='public' UNION "
                    "SELECT origin_id AS id FROM market_listings WHERE run_id=%s",
                    (run_id, run_id),
                )
                if not set(command.item_ids) <= {k["id"] for k in known}:
                    raise WorldError("The shopping task names an unknown public item.", 422)
                task_id = uuid5(UUID(str(run_id)), f"shop-task:{owner_id}:{command.task_key}")
                await conn.execute(
                    "INSERT INTO shop_tasks(id,run_id,owner_id,task_key,description,"
                    "item_ids,quantity,"
                    "max_total,owner_message_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        task_id,
                        run_id,
                        owner_id,
                        command.task_key,
                        command.description,
                        list(command.item_ids),
                        command.quantity,
                        command.max_total,
                        message_id,
                    ),
                )
            result = await row(conn, "SELECT * FROM shop_tasks WHERE id=%s", (task_id,))
            await emit(
                conn,
                run_id,
                "shopping_task_updated",
                run["current_window"],
                plain(result),
                f"shopping-task:{request_id}",
                owner_id,
            )
            return await self._save(conn, run_id, owner_id, request_id, payload, result)

    async def orders(self, conn, run_id, owner_id):
        records = await rows(
            conn,
            "SELECT o.*,t.task_key FROM shop_orders o JOIN shop_tasks t ON t.id=o.task_id "
            "WHERE o.run_id=%s AND o.owner_id=%s ORDER BY o.created_at DESC,o.id LIMIT 100",
            (run_id, owner_id),
        )
        for order in records:
            order["lines"] = await rows(
                conn,
                "SELECT l.*,r.result AS receipt FROM shop_order_lines l JOIN market_requests r "
                "ON (r.run_id,r.owner_id,r.request_id)=(l.run_id,l.owner_id,l.request_id) "
                "WHERE l.order_id=%s ORDER BY l.listing_id",
                (order["id"],),
            )
            states = {line["receipt"]["status"] for line in order["lines"]}
            order["status"] = (
                "pending"
                if "pending" in states
                else "accepted"
                if states == {"accepted"}
                else "rejected"
                if states == {"rejected"}
                else "partial"
            )
            order["quoted_total"] = sum(
                line["quantity"] * line["unit_price"] for line in order["lines"]
            )
            order["paid_total"] = sum(
                line["quantity"] * line["unit_price"]
                for line in order["lines"]
                if line["receipt"]["status"] == "accepted"
            )
        return records

    async def state(self, run_id, owner_id, *, connection=None):
        async with self.connection(connection) as conn, conn.transaction():
            await self._require(conn, run_id, owner_id)
            run = await row(
                conn, "SELECT status,current_window FROM environment_runs WHERE id=%s", (run_id,)
            )
            market = await self.market.view(run_id, owner_id, connection=conn)
            cart = await row(
                conn, "SELECT * FROM shop_carts WHERE run_id=%s AND owner_id=%s", (run_id, owner_id)
            )
            cart["lines"] = await rows(
                conn,
                "SELECT c.*,l.title,l.origin_id,l.quantity AS stock,l.unit_price AS current_price,"
                "l.version AS current_version FROM shop_cart_lines c JOIN market_listings l "
                "ON (l.run_id,l.listing_id)=(c.run_id,c.listing_id) "
                "WHERE c.run_id=%s AND c.owner_id=%s "
                "ORDER BY c.listing_id",
                (run_id, owner_id),
            )
            cart["total"] = sum(line["quantity"] * line["quoted_price"] for line in cart["lines"])
            return plain(
                {
                    "owner_id": owner_id,
                    "run": run,
                    "balance": market["balance"],
                    "consent": market["consent"],
                    "catalog": [
                        line for line in market["listings"] if line["seller_id"] != owner_id
                    ],
                    "tasks": await self.tasks(run_id, owner_id, connection=conn),
                    "cart": cart,
                    "orders": await self.orders(conn, run_id, owner_id),
                }
            )

    async def operation(self, run_id, owner_id, request_id):
        async with self.pool.connection() as conn:
            await self._require(conn, run_id, owner_id)
            found = await row(
                conn,
                "SELECT action_hash,result FROM shop_operations WHERE run_id=%s "
                "AND owner_id=%s AND request_id=%s",
                (run_id, owner_id, request_id),
            )
            if found is None:
                raise WorldError("Shopping operation not found.", 404)
            return found

    async def change_cart(
        self,
        run_id,
        owner_id,
        request_id,
        *,
        action,
        task_id=None,
        listing_id=None,
        quantity=0,
        action_hash=None,
    ):
        payload = plain(
            {"action": action, "task_id": task_id, "listing_id": listing_id, "quantity": quantity}
        )
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            await self._require(conn, run_id, owner_id)
            prior = await self._prior(conn, run_id, owner_id, request_id, payload, action_hash)
            if prior is not None:
                return prior
            self.world._active(run)
            cart = await row(
                conn,
                "SELECT * FROM shop_carts WHERE run_id=%s AND owner_id=%s FOR UPDATE",
                (run_id, owner_id),
            )
            if action == "select_task":
                task = await row(
                    conn,
                    "SELECT * FROM shop_tasks WHERE id=%s AND run_id=%s AND owner_id=%s "
                    "AND status='open'",
                    (task_id, run_id, owner_id),
                )
                if not task:
                    raise WorldError("Select one of your open shopping tasks.", 404)
                if cart["task_id"] != task_id:
                    await conn.execute(
                        "DELETE FROM shop_cart_lines WHERE run_id=%s AND owner_id=%s",
                        (run_id, owner_id),
                    )
                await conn.execute(
                    "UPDATE shop_carts SET task_id=%s,version=version+1 WHERE run_id=%s "
                    "AND owner_id=%s",
                    (task_id, run_id, owner_id),
                )
            elif action == "set_quantity":
                if type(quantity) is not int or not 0 <= quantity <= 100:
                    raise WorldError("Quantity must be an integer from zero through 100.", 422)
                selected = task_id or cart["task_id"]
                task = await row(
                    conn,
                    "SELECT * FROM shop_tasks WHERE id=%s AND run_id=%s AND owner_id=%s "
                    "AND status='open'",
                    (selected, run_id, owner_id),
                )
                if not task:
                    raise WorldError("Choose an open shopping task before changing the cart.")
                if selected != cart["task_id"]:
                    await conn.execute(
                        "DELETE FROM shop_cart_lines WHERE run_id=%s AND owner_id=%s",
                        (run_id, owner_id),
                    )
                    await conn.execute(
                        "UPDATE shop_carts SET task_id=%s WHERE run_id=%s AND owner_id=%s",
                        (selected, run_id, owner_id),
                    )
                listing = await row(
                    conn,
                    "SELECT * FROM market_listings WHERE run_id=%s AND listing_id=%s",
                    (run_id, listing_id),
                )
                if not listing or listing["seller_id"] == owner_id:
                    raise WorldError("Product not found.", 404)
                if quantity and (
                    listing["origin_id"] not in task["item_ids"] or quantity > listing["quantity"]
                ):
                    raise WorldError(
                        "The product is outside this task or has insufficient stock.", 403
                    )
                if quantity:
                    count = await row(
                        conn,
                        "SELECT count(*) AS n FROM shop_cart_lines WHERE run_id=%s "
                        "AND owner_id=%s AND listing_id<>%s",
                        (run_id, owner_id, listing_id),
                    )
                    if count["n"] >= 20:
                        raise WorldError("The cart already has 20 product lines.")
                    await conn.execute(
                        "INSERT INTO shop_cart_lines VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT "
                        "(run_id,owner_id,listing_id) DO UPDATE SET quantity=EXCLUDED.quantity,"
                        "quoted_price=EXCLUDED.quoted_price,quoted_version=EXCLUDED.quoted_version",
                        (
                            run_id,
                            owner_id,
                            listing_id,
                            quantity,
                            listing["unit_price"],
                            listing["version"],
                        ),
                    )
                else:
                    await conn.execute(
                        "DELETE FROM shop_cart_lines WHERE run_id=%s AND owner_id=%s "
                        "AND listing_id=%s",
                        (run_id, owner_id, listing_id),
                    )
                await conn.execute(
                    "UPDATE shop_carts SET version=version+1 WHERE run_id=%s AND owner_id=%s",
                    (run_id, owner_id),
                )
            else:
                raise WorldError("Unknown cart operation.", 422)
            result = await self.state(run_id, owner_id, connection=conn)
            await emit(
                conn,
                run_id,
                "shop_cart_updated",
                run["current_window"],
                {"cart_version": result["cart"]["version"], "action": action},
                f"shop-cart:{owner_id}:{request_id}",
                owner_id,
            )
            return await self._save(
                conn, run_id, owner_id, request_id, payload, result, action_hash
            )

    async def _allocated(self, conn, task_id):
        return await row(
            conn,
            "SELECT COALESCE(sum(l.quantity),0) AS quantity,"
            "COALESCE(sum(l.quantity*l.unit_price),0) AS total "
            "FROM shop_order_lines l JOIN shop_orders o ON o.id=l.order_id JOIN market_requests r "
            "ON (r.run_id,r.owner_id,r.request_id)=(l.run_id,l.owner_id,l.request_id) "
            "WHERE o.task_id=%s AND r.result->>'status' IN ('pending','accepted')",
            (task_id,),
        )

    async def checkout(self, run_id, owner_id, request_id, cart_version, *, action_hash=None):
        payload = {"action": "checkout", "cart_version": cart_version}
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            await self._require(conn, run_id, owner_id)
            prior = await self._prior(conn, run_id, owner_id, request_id, payload, action_hash)
            if prior is not None:
                return prior
            existing = await row(
                conn,
                "SELECT id FROM shop_orders WHERE run_id=%s AND owner_id=%s AND cart_version=%s",
                (run_id, owner_id, cart_version),
            )
            if existing:
                return await self._save(
                    conn,
                    run_id,
                    owner_id,
                    request_id,
                    payload,
                    {"order_id": existing["id"], "replayed_order": True},
                    action_hash,
                )
            self.world._active(run)
            state = await self.state(run_id, owner_id, connection=conn)
            cart = state["cart"]
            if cart["version"] != cart_version or not cart["lines"]:
                raise WorldError("The cart changed or is empty. Refresh before checkout.")
            task = await row(
                conn,
                "SELECT * FROM shop_tasks WHERE id=%s AND run_id=%s AND owner_id=%s "
                "AND status='open' FOR UPDATE",
                (cart["task_id"], run_id, owner_id),
            )
            if not task:
                raise WorldError("This shopping task is submitted, completed or cancelled.")
            allocated = await self._allocated(conn, task["id"])
            quantity = sum(line["quantity"] for line in cart["lines"])
            if (
                quantity + allocated["quantity"] > task["quantity"]
                or cart["total"] + allocated["total"] > task["max_total"]
            ):
                raise WorldError("Checkout exceeds the remaining task quantity or total cap.", 403)
            consent = state["consent"]
            if (
                cart["total"] > state["balance"]
                or cart["total"] > consent["buy_budget"]
                or (
                    consent["expires_window"] is not None
                    and consent["expires_window"] < run["current_window"]
                )
                or any(line["origin_id"] not in consent["buy_items"] for line in cart["lines"])
            ):
                raise WorldError("Current owner consent or funds cannot cover this purchase.", 403)
            for line in cart["lines"]:
                if line["origin_id"] not in task["item_ids"]:
                    raise WorldError("A cart product is outside this owner task.", 403)
                if (
                    line["quoted_price"] != line["current_price"]
                    or line["quoted_version"] != line["current_version"]
                ):
                    raise WorldError("A product changed. Add it again to review the current quote.")
                if line["quantity"] > line["stock"]:
                    raise WorldError("A cart product no longer has enough stock.")
            order_id = uuid5(UUID(str(run_id)), f"shop-order:{owner_id}:{cart_version}")
            await conn.execute(
                "INSERT INTO shop_orders(id,run_id,owner_id,task_id,cart_version,window_index) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (order_id, run_id, owner_id, task["id"], cart_version, run["current_window"]),
            )
            pending = []
            for line in cart["lines"]:
                action_id = uuid5(order_id, f"line:{line['listing_id']}")
                receipt = await self.market.submit_current(
                    run_id,
                    owner_id,
                    action_id,
                    "buy",
                    listing_id=line["listing_id"],
                    quantity=line["quantity"],
                    expected_version=line["quoted_version"],
                    connection=conn,
                )
                await conn.execute(
                    "INSERT INTO shop_order_lines VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        owner_id,
                        order_id,
                        action_id,
                        line["listing_id"],
                        line["origin_id"],
                        line["title"],
                        line["quantity"],
                        line["quoted_price"],
                    ),
                )
                if receipt["status"] == "pending":
                    pending.append(str(action_id))
            await conn.execute(
                "DELETE FROM shop_cart_lines WHERE run_id=%s AND owner_id=%s", (run_id, owner_id)
            )
            await conn.execute(
                "UPDATE shop_carts SET version=version+1 WHERE run_id=%s AND owner_id=%s",
                (run_id, owner_id),
            )
            await conn.execute(
                "UPDATE shop_tasks SET status='submitted' WHERE id=%s", (task["id"],)
            )
            result = {"order_id": str(order_id), "pending_ids": pending}
            await emit(
                conn,
                run_id,
                "shop_checkout",
                run["current_window"],
                result,
                f"shop-checkout:{order_id}",
                owner_id,
            )
            return await self._save(
                conn, run_id, owner_id, request_id, payload, result, action_hash
            )

    async def sync(self, run_id):
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            if not await self.enabled(conn, run_id):
                return
            tasks = await rows(
                conn, "SELECT * FROM shop_tasks WHERE run_id=%s AND status='submitted'", (run_id,)
            )
            for task in tasks:
                allocation = await self._allocated(conn, task["id"])
                pending = await row(
                    conn,
                    "SELECT 1 FROM shop_order_lines l JOIN shop_orders o ON o.id=l.order_id "
                    "JOIN market_requests r ON "
                    "(r.run_id,r.owner_id,r.request_id)=(l.run_id,l.owner_id,l.request_id) "
                    "WHERE o.task_id=%s AND r.result->>'status'='pending' LIMIT 1",
                    (task["id"],),
                )
                if pending:
                    continue
                status = "completed" if allocation["quantity"] == task["quantity"] else "open"
                await conn.execute(
                    "UPDATE shop_tasks SET status=%s WHERE id=%s", (status, task["id"])
                )
                await emit(
                    conn,
                    run_id,
                    "shopping_task_updated",
                    run["current_window"],
                    {"task_id": str(task["id"]), "status": status},
                    f"shop-task-settled:{task['id']}:{run['current_window']}",
                    task["owner_id"],
                )
