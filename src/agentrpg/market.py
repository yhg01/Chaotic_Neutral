"""Deterministic, consent-bound commerce in the simulated environment."""

from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from agentrpg.resource_schema import CommerceConsent
from agentrpg.resources import ResourceService
from agentrpg.world import WorldError, WorldService, _hash, _number, _one, _rows

MarketError = WorldError


def _version_conflict(kind, target, expected, current):
    return {
        "version_target_type": "resource" if kind == "sell" else "listing",
        "expected_version": expected,
        "current_version": current,
        "recovery": {
            "tool": "fetch_resource",
            "arguments": {"resource_id": target},
            "instruction": (
                "Fetch this exact trade target and use its version for a new request. "
                "Do not use a consent, document, or global version. Keep existing consent "
                "if it is still valid; a stale version alone does not require new permission."
            ),
        },
    }


class MarketService:
    def __init__(self, pool):
        self.pool = pool
        self.world = WorldService(pool)
        self.resources = ResourceService(pool)

    @asynccontextmanager
    async def _connection(self, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.pool.connection() as conn:
                yield conn

    async def _permission(self, conn, run_id, owner_id):
        row = await _one(
            conn,
            "SELECT * FROM market_permissions WHERE run_id=%s AND owner_id=%s",
            (run_id, owner_id),
        )
        if row is None:
            raise MarketError("Market owner not found.", 404)
        return row

    async def _prior(self, conn, run_id, owner_id, request_id, payload):
        row = await _one(
            conn,
            (
                """SELECT payload_hash, result FROM market_requests WHERE run_id=%s AND owner_id=%s
                AND request_id=%s"""
            ),
            (run_id, owner_id, request_id),
        )
        if row:
            if row["payload_hash"] != _hash(payload):
                raise MarketError("The request ID was used with a different payload.")
            return row["result"]
        return None

    async def _request(self, conn, run_id, owner_id, request_id, payload, result):
        await conn.execute(
            (
                """INSERT INTO market_requests(run_id, owner_id, request_id, payload_hash,
                request_payload, result) VALUES(%s, %s, %s, %s, %s, %s)"""
            ),
            (run_id, owner_id, request_id, _hash(payload), Jsonb(payload), Jsonb(result)),
        )

    async def set_consent(
        self,
        run_id,
        owner_id,
        buy_budget,
        buy_items,
        sell_limits,
        request_id,
        *,
        expires_window=None,
        connection=None,
    ):
        """Replace owner authority. Only trusted Actor/Director adapters call this method."""
        try:
            consent = CommerceConsent.model_validate(
                {
                    "buy_budget": buy_budget,
                    "buy_items": buy_items,
                    "sell_limits": sell_limits,
                    "expires_window": expires_window,
                }
            )
        except ValidationError as error:
            raise MarketError("Invalid commerce consent.", 422) from error
        payload = {"kind": "consent", **consent.model_dump(mode="json")}
        if connection is None:
            async with self.pool.connection() as conn:
                return await self.set_consent(
                    run_id,
                    owner_id,
                    buy_budget,
                    buy_items,
                    sell_limits,
                    request_id,
                    expires_window=expires_window,
                    connection=conn,
                )
        async with connection.transaction():
            run = await self.world._run(connection, run_id)
            await self._permission(connection, run_id, owner_id)
            old = await self._prior(connection, run_id, owner_id, request_id, payload)
            if old is not None:
                return old
            self.world._active(run)
            resources = await _rows(
                connection,
                "SELECT resource_id,origin_id,owner_id,kind FROM owner_resources WHERE run_id=%s",
                (run_id,),
            )
            item_ids = {r["resource_id"] for r in resources if r["kind"] == "item"}
            own_ids = {
                r["resource_id"]
                for r in resources
                if r["kind"] == "item" and r["owner_id"] == owner_id
            }
            if (
                set(consent.buy_items) - item_ids
                or {s.resource_id for s in consent.sell_limits} - own_ids
            ):
                raise MarketError("Commerce consent names an unknown or unowned item.", 422)
            result = await _one(
                connection,
                """UPDATE market_permissions SET buy_budget=%s, buy_items=%s, sell_limits=%s,
                version=version+1, expires_window=%s WHERE run_id=%s AND owner_id=%s RETURNING
                owner_id, buy_budget, buy_items, sell_limits, version, expires_window""",
                (
                    consent.buy_budget,
                    list(consent.buy_items),
                    Jsonb([s.model_dump() for s in consent.sell_limits]),
                    expires_window if expires_window is not None else run["max_windows"],
                    run_id,
                    owner_id,
                ),
            )
            result = {"id": str(request_id), "status": "accepted", **result}
            await self._request(connection, run_id, owner_id, request_id, payload, result)
            await self.resources._journal(connection, run_id, "commerce_consent_changed")
            await self.world._event(
                connection,
                run_id,
                run["current_window"],
                "commerce_consent_changed",
                owner_id,
                f"market:consent:{owner_id}:{request_id}",
                result,
            )
            return result

    async def view(self, run_id, owner_id, *, connection=None):
        if connection is None:
            async with self.pool.connection() as conn:
                return await self.view(run_id, owner_id, connection=conn)
        async with connection.transaction():
            run = await self.world._run(connection, run_id)
            account = await self.world._account(connection, run_id, owner_id)
            consent = await self._permission(connection, run_id, owner_id)
            listings = await self.resources._listings(connection, run_id)
            grant_event = await _one(
                connection,
                'SELECT payload,created_at,"window" FROM run_events WHERE run_id=%s '
                "AND owner_id=%s AND kind='commerce_consent_changed' "
                "ORDER BY (payload->>'version')::integer DESC LIMIT 1",
                (run_id, owner_id),
            )
            fresh_messages = (
                await _rows(
                    connection,
                    "SELECT id FROM owner_messages WHERE run_id=%s AND owner_id=%s "
                    "AND source='agent' AND created_at>%s "
                    "ORDER BY created_at DESC,id DESC LIMIT 50",
                    (run_id, owner_id, grant_event["created_at"]),
                )
                if grant_event
                else []
            )
            return {
                "owner_id": owner_id,
                "balance": account["balance"],
                "consent": {k: v for k, v in consent.items() if k not in {"run_id", "owner_id"}},
                "last_grant": (
                    {**grant_event["payload"], "window": grant_event["window"]}
                    if grant_event
                    else None
                ),
                "default_expires_window": run["max_windows"],
                "renewal_proposal_ids": [str(message["id"]) for message in fresh_messages],
                "listings": listings[:50],
                "listings_truncated": len(listings) > 50,
            }

    async def receipt(self, run_id, owner_id, request_id):
        async with self.pool.connection() as conn:
            row = await _one(
                conn,
                (
                    """SELECT result FROM market_requests WHERE run_id=%s AND owner_id=%s AND
                    request_id=%s"""
                ),
                (run_id, owner_id, request_id),
            )
            if row is None:
                raise MarketError("Market receipt not found.", 404)
            return row["result"]

    async def submit_current(
        self,
        run_id,
        owner_id,
        request_id,
        kind,
        *,
        resource_id=None,
        listing_id=None,
        quantity=1,
        unit_price=None,
        expected_version=None,
        connection=None,
    ):
        if kind not in {"sell", "buy"}:
            raise MarketError("Commerce action must be sell or buy.", 422)
        if _number(quantity, "Quantity", 1_000_000) == 0:
            raise MarketError("Quantity must be positive.", 422)
        if expected_version is not None:
            _number(expected_version, "Expected version")
        if kind == "sell":
            if (
                not isinstance(resource_id, str)
                or not resource_id
                or listing_id is not None
                or unit_price is None
            ):
                raise MarketError("Sell needs a resource ID and unit price only.", 422)
            _number(unit_price, "Unit price", 1_000_000)
            target = resource_id
        else:
            try:
                target = str(UUID(str(listing_id)))
            except (ValueError, TypeError, AttributeError):
                raise MarketError("Buy needs a valid listing ID.", 422) from None
            if resource_id is not None or unit_price is not None:
                raise MarketError("Buy uses the published listing price.", 422)
        payload = {
            "kind": kind,
            "target_id": target,
            "quantity": quantity,
            "unit_price": unit_price,
            "expected_version": expected_version,
        }
        async with self._connection(connection) as conn:
            async with conn.transaction():
                run = await self.world._run(conn, run_id)
                consent = await self._permission(conn, run_id, owner_id)
                old = await self._prior(conn, run_id, owner_id, request_id, payload)
                if old is not None:
                    return old
                self.world._active(run)
                window = run["current_window"]
                if await _one(
                    conn,
                    "SELECT 1 FROM market_settlements WHERE run_id=%s AND window_index=%s",
                    (run_id, window),
                ):
                    raise MarketError("The market decision window has already settled.")
                if kind == "sell":
                    target_row = await _one(
                        conn,
                        (
                            """SELECT kind,version FROM owner_resources
                            WHERE run_id=%s AND owner_id=%s AND resource_id=%s"""
                        ),
                        (run_id, owner_id, target),
                    )
                    if not target_row or target_row["kind"] != "item":
                        raise MarketError("Owned item not found.", 404)
                else:
                    target_row = await _one(
                        conn,
                        "SELECT version FROM market_listings WHERE run_id=%s AND listing_id=%s",
                        (run_id, UUID(target)),
                    )
                    if target_row is None:
                        raise MarketError("Listing not found.", 404)
                if await _one(
                    conn,
                    (
                        """SELECT 1 FROM market_intents WHERE run_id=%s AND owner_id=%s AND
                        window_index=%s AND target_id=%s"""
                    ),
                    (run_id, owner_id, window, target),
                ):
                    raise MarketError(
                        "Only one intent per owner and target is allowed in a window."
                    )
                result = {
                    "id": str(request_id),
                    "status": "pending",
                    "owner_id": owner_id,
                    "window": window,
                    **payload,
                }
                if expected_version is not None and expected_version != target_row["version"]:
                    # A known mismatch cannot improve by waiting for settlement. Save
                    # a final receipt but no intent, so a new corrected request can
                    # succeed in this window. Retried IDs retain this exact result.
                    result.update(
                        status="rejected",
                        reason="stale_version",
                        validation_phase="submission",
                        **_version_conflict(kind, target, expected_version, target_row["version"]),
                    )
                    await self._request(conn, run_id, owner_id, request_id, payload, result)
                    await self.world._event(
                        conn,
                        run_id,
                        window,
                        "market_settled",
                        owner_id,
                        f"market:submission_rejected:{owner_id}:{request_id}",
                        result,
                    )
                    return result
                await self._request(conn, run_id, owner_id, request_id, payload, result)
                await conn.execute(
                    (
                        """INSERT INTO market_intents(run_id, owner_id, request_id, window_index,
                        kind, target_id,  quantity, unit_price, expected_version,
                        consent_version) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
                    ),
                    (
                        run_id,
                        owner_id,
                        request_id,
                        window,
                        kind,
                        target,
                        quantity,
                        unit_price,
                        expected_version,
                        consent["version"],
                    ),
                )
                return result

    async def settle(self, run_id, window, seed, *, connection=None):
        _number(window, "Window", 100)
        if connection is None:
            async with self.pool.connection() as conn:
                return await self.settle(run_id, window, seed, connection=conn)
        async with connection.transaction():
            run = await self.world._run(connection, run_id)
            prior = await _one(
                connection,
                "SELECT * FROM market_settlements WHERE run_id=%s AND window_index=%s",
                (run_id, window),
            )
            if prior:
                if prior["seed"] != seed:
                    raise MarketError("The market window was settled with a different seed.")
                return prior["results"]
            self.world._active(run, window)
            if seed != run["seed"]:
                raise MarketError("The settlement seed must match the frozen run seed.")
            intents = await _rows(
                connection,
                "SELECT * FROM market_intents WHERE run_id=%s AND window_index=%s",
                (run_id, window),
            )
            intents.sort(
                key=lambda i: (
                    0 if i["kind"] == "sell" else 1,
                    _hash([seed, window, i["owner_id"]]),
                    i["owner_id"],
                    i["target_id"],
                )
            )
            # Expected versions refer to the state at the start of the settlement.
            resource_versions = {
                r["resource_id"]: r["version"]
                for r in await _rows(
                    connection,
                    "SELECT resource_id,version FROM owner_resources WHERE run_id=%s",
                    (run_id,),
                )
            }
            listing_versions = {
                str(r["listing_id"]): r["version"]
                for r in await _rows(
                    connection,
                    "SELECT listing_id,version FROM market_listings WHERE run_id=%s",
                    (run_id,),
                )
            }
            results = []
            for intent in intents:
                consent = await self._permission(connection, run_id, intent["owner_id"])
                reason = None
                if consent["version"] != intent["consent_version"]:
                    reason = "consent_changed"
                elif consent["expires_window"] is not None and consent["expires_window"] < window:
                    reason = "consent_expired"
                versions = resource_versions if intent["kind"] == "sell" else listing_versions
                if (
                    reason is None
                    and intent["expected_version"] is not None
                    and intent["expected_version"] != versions.get(intent["target_id"])
                ):
                    reason = "stale_version"
                extra = {}
                if reason == "stale_version":
                    extra = {
                        "validation_phase": "settlement",
                        **_version_conflict(
                            intent["kind"],
                            intent["target_id"],
                            intent["expected_version"],
                            versions.get(intent["target_id"]),
                        ),
                    }
                if reason is None:
                    if intent["kind"] == "sell":
                        reason, extra = await self._sell(connection, run_id, intent, consent)
                    else:
                        reason, extra = await self._buy(connection, run_id, intent, consent)
                result = {
                    "id": str(intent["request_id"]),
                    "owner_id": intent["owner_id"],
                    "window": window,
                    "kind": intent["kind"],
                    "target_id": intent["target_id"],
                    "quantity": intent["quantity"],
                    "status": "rejected" if reason else "accepted",
                    "reason": reason,
                    **extra,
                }
                await connection.execute(
                    (
                        """UPDATE market_requests SET result=%s WHERE run_id=%s AND owner_id=%s AND
                        request_id=%s"""
                    ),
                    (Jsonb(result), run_id, intent["owner_id"], intent["request_id"]),
                )
                await self.world._event(
                    connection,
                    run_id,
                    window,
                    "market_settled",
                    intent["owner_id"],
                    f"market:settled:{intent['owner_id']}:{intent['request_id']}",
                    result,
                )
                results.append(result)
            await connection.execute(
                (
                    """INSERT INTO market_settlements(run_id, window_index, seed, results)
                    VALUES(%s, %s, %s, %s)"""
                ),
                (run_id, window, seed, Jsonb(results)),
            )
            return results

    async def _sell(self, conn, run_id, intent, consent):
        owner, target = intent["owner_id"], intent["target_id"]
        resource = await _one(
            conn,
            "SELECT * FROM owner_resources WHERE run_id=%s AND resource_id=%s AND owner_id=%s",
            (run_id, target, owner),
        )
        limit = next((r for r in consent["sell_limits"] if r["resource_id"] == target), None)
        if not resource or resource["kind"] != "item":
            return "owned_item_missing", {}
        if not limit:
            return "sell_consent_required", {}
        if intent["unit_price"] < limit["min_unit_price"]:
            return "minimum_price_not_met", {}
        committed = await _one(
            conn,
            """SELECT COALESCE(sum(quantity), 0) AS quantity FROM market_listings WHERE run_id=%s
            AND resource_id=%s AND consent_version=%s""",
            (run_id, target, consent["version"]),
        )
        available = min(resource["quantity"], limit["quantity"]) - committed["quantity"]
        if intent["quantity"] > available:
            return "quantity_unavailable", {}
        listing_id = uuid5(UUID(str(run_id)), f"listing:{owner}:{intent['request_id']}")
        await conn.execute(
            """INSERT INTO market_listings(run_id, listing_id, seller_id, resource_id, origin_id,
            title,  quantity, unit_price, consent_version) VALUES(%s, %s, %s, %s, %s, %s, %s,
            %s, %s)""",
            (
                run_id,
                listing_id,
                owner,
                target,
                resource["origin_id"],
                resource["title"],
                intent["quantity"],
                intent["unit_price"],
                consent["version"],
            ),
        )
        await self.resources._journal(conn, run_id, "listing_created")
        await self.world._event(
            conn,
            run_id,
            intent["window_index"],
            "market_changed",
            None,
            f"market:listing:{listing_id}",
            {
                "listing_id": str(listing_id),
                "resource_id": target,
                "seller_id": owner,
                "quantity": intent["quantity"],
                "unit_price": intent["unit_price"],
            },
        )
        return None, {"listing_id": str(listing_id), "unit_price": intent["unit_price"]}

    async def _buy(self, conn, run_id, intent, consent):
        buyer = intent["owner_id"]
        listing = await _one(
            conn,
            "SELECT * FROM market_listings WHERE run_id=%s AND listing_id=%s",
            (run_id, UUID(intent["target_id"])),
        )
        if not listing:
            return "listing_missing", {}
        seller = listing["seller_id"]
        if buyer == seller:
            return "self_purchase_not_allowed", {}
        seller_consent = await self._permission(conn, run_id, seller)
        if seller_consent["version"] != listing["consent_version"]:
            return "seller_consent_changed", {}
        if (
            seller_consent["expires_window"] is not None
            and seller_consent["expires_window"] < intent["window_index"]
        ):
            return "seller_consent_expired", {}
        seller_limit = next(
            (
                r
                for r in seller_consent["sell_limits"]
                if r["resource_id"] == listing["resource_id"]
            ),
            None,
        )
        if not seller_limit:
            return "sell_consent_required", {}
        if (
            listing["resource_id"] not in consent["buy_items"]
            and listing["origin_id"] not in consent["buy_items"]
        ):
            return "buy_consent_required", {}
        resource = await _one(
            conn,
            "SELECT * FROM owner_resources WHERE run_id=%s AND resource_id=%s",
            (run_id, listing["resource_id"]),
        )
        quantity = intent["quantity"]
        if quantity > min(resource["quantity"], listing["quantity"], seller_limit["quantity"]):
            return "quantity_unavailable", {}
        amount = quantity * listing["unit_price"]
        account = await self.world._account(conn, run_id, buyer)
        if amount > consent["buy_budget"]:
            return "buy_budget_exceeded", {}
        if amount > account["balance"]:
            return "insufficient_balance", {}
        # All checks precede effects. The run transaction makes credits, stock,
        # permission budgets, receipts, ledger, and delivery events atomic.
        buyer_account = await _one(
            conn,
            (
                """UPDATE world_accounts SET balance=balance-%s WHERE run_id=%s AND owner_id=%s
                RETURNING balance"""
            ),
            (amount, run_id, buyer),
        )
        seller_account = await _one(
            conn,
            (
                """UPDATE world_accounts SET balance=balance+%s WHERE run_id=%s AND owner_id=%s
                RETURNING balance"""
            ),
            (amount, run_id, seller),
        )
        await conn.execute(
            (
                """UPDATE market_permissions SET buy_budget=buy_budget-%s WHERE run_id=%s AND
                owner_id=%s"""
            ),
            (amount, run_id, buyer),
        )
        seller_limit["quantity"] -= quantity
        await conn.execute(
            "UPDATE market_permissions SET sell_limits=%s WHERE run_id=%s AND owner_id=%s",
            (Jsonb(seller_consent["sell_limits"]), run_id, seller),
        )
        await conn.execute(
            (
                """UPDATE owner_resources SET quantity=quantity-%s, version=version+1 WHERE
                run_id=%s AND resource_id=%s"""
            ),
            (quantity, run_id, resource["resource_id"]),
        )
        await conn.execute(
            (
                """UPDATE market_listings SET quantity=quantity-%s, version=version+1 WHERE
                run_id=%s AND listing_id=%s"""
            ),
            (quantity, run_id, listing["listing_id"]),
        )
        holding = str(uuid5(UUID(str(run_id)), f"holding:{buyer}:{listing['origin_id']}"))
        # Ownership of an item transfers. Private notes and sharing permissions do not.
        await conn.execute(
            (
                """INSERT INTO owner_resources(run_id, resource_id, owner_id, origin_id, title,
                kind, quantity) VALUES(%s, %s, %s, %s, %s, 'item', %s) ON CONFLICT(run_id,
                resource_id) DO UPDATE SET quantity=owner_resources.quantity+EXCLUDED.quantity,
                version=owner_resources.version+1"""
            ),
            (run_id, holding, buyer, listing["origin_id"], listing["title"], quantity),
        )
        await conn.execute(
            (
                """INSERT INTO market_ledger(run_id, owner_id, request_id, seller_id, listing_id,
                resource_id,  quantity, amount, buyer_balance, seller_balance, window_index)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
            ),
            (
                run_id,
                buyer,
                intent["request_id"],
                seller,
                listing["listing_id"],
                holding,
                quantity,
                amount,
                buyer_account["balance"],
                seller_account["balance"],
                intent["window_index"],
            ),
        )
        # Journal both accounts through the existing transport journal as well.
        await self.world._journal(conn, run_id, "market_purchase", buyer)
        await self.world._journal(conn, run_id, "market_sale", seller)
        await self.resources._journal(conn, run_id, "purchase")
        public = {
            "listing_id": str(listing["listing_id"]),
            "buyer_id": buyer,
            "seller_id": seller,
            "quantity": quantity,
            "amount": amount,
        }
        await self.world._event(
            conn,
            run_id,
            intent["window_index"],
            "market_changed",
            None,
            f"market:purchase:{buyer}:{intent['request_id']}",
            public,
        )
        await self.world._event(
            conn,
            run_id,
            intent["window_index"],
            "resource_updated",
            seller,
            f"market:seller:{buyer}:{intent['request_id']}",
            public,
        )
        return None, {**public, "resource_id": holding, "balance": buyer_account["balance"]}

    async def ledger(self, run_id):
        return (await self.resources.snapshot(run_id))["ledger"]

    async def journal(self, run_id):
        return await self.resources.journal(run_id)

    replay = staticmethod(ResourceService.replay)
