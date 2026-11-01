"""Authoritative, staged reservations with owner consent and exact receipts.

The Director owns time. This service takes a short run-row lock for domain
changes, never for board writes or model calls. Request arrival order does not
select winners. Recorded state patches support replay without a language model.
"""

import copy
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


class WorldError(ValueError):
    """Safe domain failure that can be returned to a client."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _number(value: Any, name: str, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise WorldError(f"{name} must be an integer from 0 to {maximum}.", 422)
    return value


async def _rows(connection: AsyncConnection, query: str, params=()) -> list[dict]:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(query, params)
        return await cursor.fetchall()


async def _one(connection: AsyncConnection, query: str, params=()) -> dict | None:
    rows = await _rows(connection, query, params)
    return rows[0] if rows else None


class WorldService:
    """World operations for trusted, identity-scoped adapters and the Director."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def _run(self, connection, run_id) -> dict:
        run = await _one(
            connection, "SELECT * FROM environment_runs WHERE id = %s FOR UPDATE", (run_id,)
        )
        if run is None:
            raise WorldError("Run not found.", 404)
        return run

    @staticmethod
    def _active(run: dict, window: int | None = None):
        if run["status"] != "running":
            raise WorldError("The run is not running.")
        if window is not None and run["current_window"] != window:
            raise WorldError("The decision window is not current.")

    async def _account(self, connection, run_id, owner_id) -> dict:
        account = await _one(
            connection,
            "SELECT * FROM world_accounts WHERE run_id = %s AND owner_id = %s",
            (run_id, owner_id),
        )
        if account is None:
            raise WorldError("Owner not found in this world.", 404)
        return account

    async def _prior(
        self, connection, run_id, owner_id, request_id, payload, *, legacy_payload=None
    ) -> dict | None:
        old = await _one(
            connection,
            """SELECT payload_hash, result FROM world_requests
               WHERE run_id = %s AND owner_id = %s AND request_id = %s""",
            (run_id, owner_id, request_id),
        )
        if old is not None:
            legacy_matches = (
                legacy_payload is not None
                and "consent_kinds" not in old["result"]
                and old["payload_hash"] == _hash(legacy_payload)
            )
            if old["payload_hash"] != _hash(payload) and not legacy_matches:
                raise WorldError("The request ID was used with a different payload.")
            return old["result"]
        return None

    async def _request(self, connection, run_id, owner_id, request_id, payload, result):
        await connection.execute(
            """INSERT INTO world_requests(run_id, owner_id, request_id, payload_hash,
                      request_payload, result)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, owner_id, request_id, _hash(payload), Jsonb(payload), Jsonb(result)),
        )

    async def _event(self, connection, run_id, window, kind, owner_id, key, payload):
        await connection.execute(
            """INSERT INTO run_events(id, run_id, kind, "window", payload, owner_id, operation_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (uuid4(), run_id, kind, window, Jsonb(payload), owner_id, key),
        )

    async def initialize(self, run_id: UUID, spec: dict, *, connection=None) -> dict:
        """Create the world once. An optional caller transaction makes run creation atomic."""
        if connection is None:
            async with self.pool.connection() as owned:
                async with owned.transaction():
                    return await self.initialize(run_id, spec, connection=owned)
        async with connection.transaction():
            await self._run(connection, run_id)
            owners = [owner["id"] for owner in spec["owners"]]
            world = spec["world"]
            assets = world["assets"]
            balances = world["balances"]
            if not 1 <= len(owners) <= 15 or len(owners) != len(set(owners)):
                raise WorldError("The world needs 1 to 15 different owners.", 422)
            if set(balances) != set(owners):
                raise WorldError("Balances must identify all owners and no other owners.", 422)
            asset_ids = [asset["id"] for asset in assets]
            if len(set(asset_ids)) != len(asset_ids):
                raise WorldError("Asset IDs must be unique.", 422)
            for balance in balances.values():
                _number(balance, "Balance")
            for asset in assets:
                _number(asset.get("price", 0), "Price")
                if _number(asset.get("capacity", 1), "Capacity", 15) == 0:
                    raise WorldError("Asset capacity must be positive.", 422)
                if asset.get("initial_holder") not in [None, *owners]:
                    raise WorldError("An initial holder must be an owner in this run.", 422)
            payload_hash = _hash({"owners": sorted(owners), "world": world})
            prior = await _one(connection, "SELECT * FROM world_state WHERE run_id = %s", (run_id,))
            if prior:
                if prior["initialization_hash"] != payload_hash:
                    raise WorldError("The world was initialized with a different specification.")
                return await self._snapshot(connection, run_id)
            participants = await _rows(
                connection, "SELECT owner_id FROM run_participants WHERE run_id = %s", (run_id,)
            )
            if {row["owner_id"] for row in participants} != set(owners):
                raise WorldError("World owners must match the run participants.", 422)
            await connection.execute(
                "INSERT INTO world_state(run_id, initialization_hash) VALUES (%s, %s)",
                (run_id, payload_hash),
            )
            for owner in owners:
                await connection.execute(
                    "INSERT INTO world_accounts(run_id, owner_id, balance) VALUES (%s, %s, %s)",
                    (run_id, owner, balances[owner]),
                )
            for asset in assets:
                await connection.execute(
                    """INSERT INTO world_assets(run_id, asset_id, name, price, capacity)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (
                        run_id,
                        asset["id"],
                        asset.get("name", asset["id"]),
                        asset.get("price", 0),
                        asset.get("capacity", 1),
                    ),
                )
                if asset.get("initial_holder") is not None:
                    await connection.execute(
                        """INSERT INTO world_reservations(run_id, owner_id, asset_id, paid)
                           VALUES (%s, %s, %s, 0)""",
                        (run_id, asset["initial_holder"], asset["id"]),
                    )
            snapshot = await self._snapshot(connection, run_id)
            await connection.execute(
                """INSERT INTO world_journal(run_id, revision, kind, payload)
                   VALUES (%s, 0, 'initialized', %s)""",
                (run_id, Jsonb(snapshot)),
            )
            return snapshot

    async def _snapshot(self, connection, run_id) -> dict:
        state = await _one(
            connection, "SELECT treasury, revision FROM world_state WHERE run_id = %s", (run_id,)
        )
        if state is None:
            raise WorldError("World not found.", 404)
        return {
            **state,
            "accounts": await _rows(
                connection,
                """SELECT owner_id, balance, consent_budget, consent_assets, consent_version,
                          consent_expires_window, consent_kinds FROM world_accounts
                   WHERE run_id = %s ORDER BY owner_id""",
                (run_id,),
            ),
            "assets": await _rows(
                connection,
                """SELECT asset_id, name, price, capacity, version FROM world_assets
                   WHERE run_id = %s ORDER BY asset_id""",
                (run_id,),
            ),
            "reservations": await _rows(
                connection,
                """SELECT owner_id, asset_id, paid FROM world_reservations
                   WHERE run_id = %s ORDER BY owner_id, asset_id""",
                (run_id,),
            ),
        }

    async def snapshot(self, run_id: UUID) -> dict:
        """Administrative snapshot. Do not send this to an Actor or assistant."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                # One short lock provides a consistent snapshot across the domain tables.
                await self._run(connection, run_id)
                return await self._snapshot(connection, run_id)

    async def view(self, run_id: UUID, owner_id: str) -> dict:
        """Return one owner's account and public resource state."""
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await self._run(connection, run_id)
                await self._account(connection, run_id, owner_id)
                snapshot = await self._snapshot(connection, run_id)
                return {
                    "owner_id": owner_id,
                    "account": next(a for a in snapshot["accounts"] if a["owner_id"] == owner_id),
                    "assets": snapshot["assets"],
                    "reservations": [
                        {"owner_id": item["owner_id"], "asset_id": item["asset_id"]}
                        for item in snapshot["reservations"]
                    ],
                }

    async def _journal(self, connection, run_id, kind, owner_id, asset_id=None):
        revision = await _one(
            connection,
            "UPDATE world_state SET revision = revision + 1 WHERE run_id = %s RETURNING revision",
            (run_id,),
        )
        state = await self._snapshot(connection, run_id)
        patch = {
            "treasury": state["treasury"],
            "account": next(a for a in state["accounts"] if a["owner_id"] == owner_id),
        }
        if asset_id is not None:
            patch["asset"] = next(a for a in state["assets"] if a["asset_id"] == asset_id)
            patch["reservation_key"] = [owner_id, asset_id]
            patch["reservation"] = next(
                (
                    r
                    for r in state["reservations"]
                    if r["owner_id"] == owner_id and r["asset_id"] == asset_id
                ),
                None,
            )
        await connection.execute(
            "INSERT INTO world_journal(run_id, revision, kind, payload) VALUES (%s, %s, %s, %s)",
            (run_id, revision["revision"], kind, Jsonb(patch)),
        )

    async def set_consent(
        self,
        run_id: UUID,
        owner_id: str,
        budget: int,
        allowed_assets: list[str],
        request_id: UUID,
        *,
        expires_window: int | None = None,
        allowed_kinds: list[str] | tuple[str, ...] = ("reserve",),
        connection: AsyncConnection | None = None,
    ) -> dict:
        """Apply an explicit owner grant or withdrawal. Assistants cannot call this."""
        _number(budget, "Budget")
        if expires_window is not None:
            _number(expires_window, "Consent expiry", 100)
        if not isinstance(allowed_assets, (list, tuple)) or not all(
            isinstance(value, str) for value in allowed_assets
        ):
            raise WorldError("Allowed assets must be a list of asset IDs.", 422)
        allowed_assets = sorted(set(allowed_assets))
        if not isinstance(allowed_kinds, (list, tuple)) or not all(
            isinstance(kind, str) and kind in {"reserve", "cancel"} for kind in allowed_kinds
        ):
            raise WorldError("Allowed kinds must contain only reserve or cancel.", 422)
        allowed_kinds = sorted(set(allowed_kinds))
        if not allowed_assets:
            if budget:
                raise WorldError("A withdrawal must have a zero budget.", 422)
            allowed_kinds = []
        elif not allowed_kinds:
            raise WorldError("An asset grant must allow at least one action kind.", 422)
        payload = {
            "kind": "consent",
            "budget": budget,
            "assets": allowed_assets,
            "expires_window": expires_window,
            "allowed_kinds": allowed_kinds,
        }
        if connection is None:
            async with self.pool.connection() as owned:
                return await self.set_consent(
                    run_id,
                    owner_id,
                    budget,
                    allowed_assets,
                    request_id,
                    expires_window=expires_window,
                    allowed_kinds=allowed_kinds,
                    connection=owned,
                )
        async with connection.transaction():
            run = await self._run(connection, run_id)
            await self._account(connection, run_id, owner_id)
            # Old consent receipts predate explicit action kinds. Only their
            # default reserve grant (or withdrawal) can use the original hash.
            legacy = (
                {key: value for key, value in payload.items() if key != "allowed_kinds"}
                if allowed_kinds in (["reserve"], [])
                else None
            )
            old = await self._prior(
                connection, run_id, owner_id, request_id, payload, legacy_payload=legacy
            )
            if old is not None:
                return old
            self._active(run)
            assets = await _rows(
                connection, "SELECT asset_id FROM world_assets WHERE run_id = %s", (run_id,)
            )
            if set(allowed_assets) - {a["asset_id"] for a in assets}:
                raise WorldError("Consent contains an unknown asset.", 422)
            effective_expiry = run["max_windows"] if expires_window is None else expires_window
            account = await _one(
                connection,
                """UPDATE world_accounts SET consent_budget = %s, consent_assets = %s,
                          consent_kinds = %s,
                          consent_version = consent_version + 1, consent_expires_window = %s
                   WHERE run_id = %s AND owner_id = %s
                   RETURNING owner_id, consent_budget, consent_assets, consent_version,
                                 consent_expires_window, consent_kinds""",
                (budget, allowed_assets, allowed_kinds, effective_expiry, run_id, owner_id),
            )
            result = {"id": str(request_id), "status": "accepted", **account}
            await self._request(connection, run_id, owner_id, request_id, payload, result)
            await self._journal(connection, run_id, "consent_changed", owner_id)
            await self._event(
                connection,
                run_id,
                run["current_window"],
                "consent_changed",
                owner_id,
                f"world:consent:{owner_id}:{request_id}",
                result,
            )
            return result

    async def submit(
        self,
        run_id: UUID,
        owner_id: str,
        window: int,
        request_id: UUID,
        kind: str,
        asset_id: str,
        expected_version: int | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> dict:
        """Stage one owner/asset intent for a window; it has no immediate world effect."""
        _number(window, "Window", 100)
        if kind not in {"reserve", "cancel"}:
            raise WorldError("Action must be reserve or cancel.", 422)
        if expected_version is not None:
            _number(expected_version, "Expected version")
        payload = {
            "kind": kind,
            "asset_id": asset_id,
            "window": window,
            "expected_version": expected_version,
        }
        if connection is None:
            async with self.pool.connection() as owned:
                return await self.submit(
                    run_id,
                    owner_id,
                    window,
                    request_id,
                    kind,
                    asset_id,
                    expected_version,
                    connection=owned,
                )
        async with connection.transaction():
            run = await self._run(connection, run_id)
            account = await self._account(connection, run_id, owner_id)
            old = await self._prior(connection, run_id, owner_id, request_id, payload)
            if old is not None:
                return old
            self._active(run, window)
            settled = await _one(
                connection,
                "SELECT 1 FROM world_settlements WHERE run_id = %s AND window_index = %s",
                (run_id, window),
            )
            if settled:
                raise WorldError("The decision window has already settled.")
            asset = await _one(
                connection,
                "SELECT asset_id FROM world_assets WHERE run_id = %s AND asset_id = %s",
                (run_id, asset_id),
            )
            if asset is None:
                raise WorldError("Asset not found in this world.", 404)
            duplicate = await _one(
                connection,
                """SELECT 1 FROM world_intents WHERE run_id = %s AND owner_id = %s
                   AND window_index = %s AND asset_id = %s""",
                (run_id, owner_id, window, asset_id),
            )
            if duplicate:
                raise WorldError("Only one intent per owner and asset is allowed in a window.")
            result = {
                "id": str(request_id),
                "owner_id": owner_id,
                "status": "pending",
                "kind": kind,
                "asset_id": asset_id,
                "window": window,
                "consent_version": account["consent_version"],
            }
            await self._request(connection, run_id, owner_id, request_id, payload, result)
            await connection.execute(
                """INSERT INTO world_intents(run_id, owner_id, request_id, window_index, kind,
                          asset_id, expected_version, consent_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    owner_id,
                    request_id,
                    window,
                    kind,
                    asset_id,
                    expected_version,
                    account["consent_version"],
                ),
            )
            return result

    async def submit_current(
        self,
        run_id: UUID,
        owner_id: str,
        request_id: UUID,
        kind: str,
        asset_id: str,
        expected_version: int | None = None,
    ) -> dict:
        """Stage a new intent now, or validate a retry against its original window.

        HTTP tools do not supply the simulation window. Resolve it while holding
        the same lock used for submission, so advancing time cannot change a
        request's identity. Every retry still checks the complete payload hash.
        """
        async with self.pool.connection() as connection:
            async with connection.transaction():
                run = await self._run(connection, run_id)
                original = await _one(
                    connection,
                    """SELECT window_index FROM world_intents
                       WHERE run_id = %s AND owner_id = %s AND request_id = %s""",
                    (run_id, owner_id, request_id),
                )
                window = original["window_index"] if original else run["current_window"]
                return await self.submit(
                    run_id,
                    owner_id,
                    window,
                    request_id,
                    kind,
                    asset_id,
                    expected_version,
                    connection=connection,
                )

    async def receipt(self, run_id: UUID, owner_id: str, request_id: UUID) -> dict:
        async with self.pool.connection() as connection:
            result = await _one(
                connection,
                """SELECT result FROM world_requests
                   WHERE run_id = %s AND owner_id = %s AND request_id = %s""",
                (run_id, owner_id, request_id),
            )
            if result is None:
                raise WorldError("Receipt not found.", 404)
            return result["result"]

    async def settle(self, run_id: UUID, window: int, seed: int) -> list[dict]:
        """Resolve the frozen window atomically, using participant ranks for contention."""
        _number(window, "Window", 100)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                run = await self._run(connection, run_id)
                prior = await _one(
                    connection,
                    "SELECT * FROM world_settlements WHERE run_id = %s AND window_index = %s",
                    (run_id, window),
                )
                if prior:
                    if prior["seed"] != seed:
                        raise WorldError("The window was settled with a different seed.")
                    return prior["results"]
                self._active(run, window)
                if run["seed"] != seed:
                    raise WorldError("The settlement seed must match the frozen run seed.")
                intents = await _rows(
                    connection,
                    "SELECT * FROM world_intents WHERE run_id = %s AND window_index = %s",
                    (run_id, window),
                )
                versions = {
                    a["asset_id"]: a["version"]
                    for a in await _rows(
                        connection,
                        "SELECT asset_id, version FROM world_assets WHERE run_id = %s",
                        (run_id,),
                    )
                }
                # All cancellations precede reservations. Each owner has one fixed rank;
                # choosing another UUID or flooding requests cannot buy a better rank.
                intents.sort(
                    key=lambda i: (
                        0 if i["kind"] == "cancel" else 1,
                        _hash([seed, window, i["owner_id"]]),
                        i["owner_id"],
                        i["asset_id"],
                    )
                )
                results = []
                for intent in intents:
                    result = await self._settle_intent(connection, run_id, intent, versions)
                    results.append(result)
                await connection.execute(
                    """INSERT INTO world_settlements(run_id, window_index, seed, results)
                       VALUES (%s, %s, %s, %s)""",
                    (run_id, window, seed, Jsonb(results)),
                )
                return results

    async def _settle_intent(self, connection, run_id, intent, versions) -> dict:
        owner_id, asset_id = intent["owner_id"], intent["asset_id"]
        request_id, window = intent["request_id"], intent["window_index"]
        account = await self._account(connection, run_id, owner_id)
        asset = await _one(
            connection,
            "SELECT * FROM world_assets WHERE run_id = %s AND asset_id = %s",
            (run_id, asset_id),
        )
        reservations = await _rows(
            connection,
            "SELECT owner_id, paid FROM world_reservations WHERE run_id = %s AND asset_id = %s",
            (run_id, asset_id),
        )
        own_reservation = next((r for r in reservations if r["owner_id"] == owner_id), None)
        reason = None
        if account["consent_version"] != intent["consent_version"]:
            reason = "consent_changed"
        elif asset_id not in account["consent_assets"]:
            reason = "consent_required"
        elif intent["kind"] not in account["consent_kinds"]:
            reason = "action_not_authorized"
        elif (
            account["consent_expires_window"] is None or account["consent_expires_window"] < window
        ):
            reason = "consent_expired"
        elif (
            intent["expected_version"] is not None
            and intent["expected_version"] != versions[asset_id]
        ):
            reason = "stale_asset_version"
        elif intent["kind"] == "cancel":
            if own_reservation is None:
                reason = "no_owned_reservation"
        elif own_reservation is not None:
            reason = "already_reserved"
        elif len(reservations) >= asset["capacity"]:
            reason = "capacity_unavailable"
        elif account["consent_budget"] < asset["price"]:
            reason = "consent_budget_exceeded"
        elif account["balance"] < asset["price"]:
            reason = "insufficient_balance"
        result = {
            "id": str(request_id),
            "owner_id": owner_id,
            "kind": intent["kind"],
            "asset_id": asset_id,
            "window": window,
            "status": "rejected" if reason else "accepted",
            "reason": reason,
            "consent_version": intent["consent_version"],
        }
        if reason is None:
            if intent["kind"] == "reserve":
                amount = -asset["price"]
                await connection.execute(
                    """INSERT INTO world_reservations(run_id, owner_id, asset_id, paid)
                       VALUES (%s, %s, %s, %s)""",
                    (run_id, owner_id, asset_id, asset["price"]),
                )
            else:
                amount = own_reservation["paid"]
                await connection.execute(
                    """DELETE FROM world_reservations
                       WHERE run_id = %s AND owner_id = %s AND asset_id = %s""",
                    (run_id, owner_id, asset_id),
                )
            updated_account = await _one(
                connection,
                """UPDATE world_accounts SET balance = balance + %s,
                          consent_budget = consent_budget + %s
                   WHERE run_id = %s AND owner_id = %s RETURNING balance""",
                # Refunds restore credits, but never increase the owner's authorized spend.
                (amount, min(amount, 0), run_id, owner_id),
            )
            treasury = await _one(
                connection,
                """UPDATE world_state SET treasury = treasury - %s
                   WHERE run_id = %s RETURNING treasury""",
                (amount, run_id),
            )
            await connection.execute(
                """UPDATE world_assets SET version = version + 1
                   WHERE run_id = %s AND asset_id = %s""",
                (run_id, asset_id),
            )
            result["amount"] = amount
            result["balance"] = updated_account["balance"]
            await connection.execute(
                """INSERT INTO world_ledger(id, run_id, owner_id, request_id, asset_id, amount,
                          balance_after, treasury_after, window_index)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    uuid4(),
                    run_id,
                    owner_id,
                    request_id,
                    asset_id,
                    amount,
                    updated_account["balance"],
                    treasury["treasury"],
                    window,
                ),
            )
            await self._journal(connection, run_id, intent["kind"], owner_id, asset_id)
            await self._event(
                connection,
                run_id,
                window,
                "world_changed",
                None,
                f"world:changed:{owner_id}:{request_id}",
                {"asset_id": asset_id, "owner_id": owner_id, "kind": intent["kind"]},
            )
        await connection.execute(
            """UPDATE world_requests SET result = %s
               WHERE run_id = %s AND owner_id = %s AND request_id = %s""",
            (Jsonb(result), run_id, owner_id, request_id),
        )
        await self._event(
            connection,
            run_id,
            window,
            "reservation_settled",
            owner_id,
            f"world:settled:{owner_id}:{request_id}",
            result,
        )
        return result

    async def journal(self, run_id: UUID) -> list[dict]:
        """Administrative state patches. Private fields are never sent as public events."""
        async with self.pool.connection() as connection:
            records = await _rows(
                connection,
                """SELECT revision, kind, payload FROM world_journal
                   WHERE run_id = %s ORDER BY revision""",
                (run_id,),
            )
            if not records:
                raise WorldError("World not found.", 404)
            return records

    @staticmethod
    def replay(records: list[dict]) -> dict:
        """Rebuild the authoritative state from recorded patches, without model calls."""
        if not records or records[0]["revision"] != 0 or records[0]["kind"] != "initialized":
            raise WorldError("Replay must start with the initial world.", 422)
        state = copy.deepcopy(records[0]["payload"])
        # Migration 005 restricts old grants to reservations. Normalize older
        # journal records by that same rule so persisted traces remain replayable.
        for account in state["accounts"]:
            account.setdefault("consent_kinds", ["reserve"] if account["consent_assets"] else [])
        for record in records[1:]:
            if record["revision"] != state["revision"] + 1:
                raise WorldError("Replay contains a missing or repeated revision.", 422)
            patch = copy.deepcopy(record["payload"])
            patch["account"].setdefault(
                "consent_kinds", ["reserve"] if patch["account"]["consent_assets"] else []
            )
            state["revision"] = record["revision"]
            state["treasury"] = patch["treasury"]
            state["accounts"] = [
                copy.deepcopy(patch["account"])
                if a["owner_id"] == patch["account"]["owner_id"]
                else a
                for a in state["accounts"]
            ]
            if "asset" in patch:
                state["assets"] = [
                    copy.deepcopy(patch["asset"])
                    if a["asset_id"] == patch["asset"]["asset_id"]
                    else a
                    for a in state["assets"]
                ]
                state["reservations"] = [
                    r
                    for r in state["reservations"]
                    if [r["owner_id"], r["asset_id"]] != patch["reservation_key"]
                ]
                if patch["reservation"] is not None:
                    state["reservations"].append(copy.deepcopy(patch["reservation"]))
                state["reservations"].sort(key=lambda r: (r["owner_id"], r["asset_id"]))
        return state
