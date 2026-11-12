"""Journalled balance changes and online card limits for clocked runs.

Every change uses the run-row lock, updates world_accounts and appends a world journal
patch, as transfers do, so WorldService.replay still rebuilds the authoritative state.
A caller passes its own connection. Each function opens a savepoint when the caller
already has a transaction, so a rejected change leaves that transaction usable.
"""

from agentrpg.world import WorldError, WorldService, _hash, _one

MAX_AMOUNT = 1_000_000_000
_WORLD = WorldService(None)


class SiteError(WorldError):
    """A refused web or money effect. A plan records this and keeps its other effects."""


def clock_section(run: dict) -> dict:
    """Return the frozen clock settings of a run row, or an empty dict for a window run."""
    return (run.get("scenario") or {}).get("clock") or {}


async def sim_now(conn, run_id) -> int:
    """Return story seconds from clock_state, or 0 for a run with no clock."""
    value = await _one(conn, "SELECT sim_s FROM clock_state WHERE run_id=%s", (run_id,))
    return value["sim_s"] if value else 0


async def record_event(conn, run_id, kind, window, payload, key, owner_id=None):
    # The Director module imports the scenario schema, which can import site specs.
    from agentrpg.director import emit

    return await emit(conn, run_id, kind, window, payload, key, owner_id)


def _operation_key(value) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 250:
        raise SiteError("An operation key of 1 to 250 characters is required.", 422)
    return value


def _amount(value, name) -> int:
    if type(value) is not int or not 1 <= value <= MAX_AMOUNT:
        raise SiteError(f"{name} must be an integer from 1 to {MAX_AMOUNT}.", 422)
    return value


def _receipt(entry: dict) -> dict:
    return {
        "status": "accepted",
        "operation_key": entry["operation_key"],
        "owner_id": entry["owner_id"],
        "delta": entry["delta"],
        "balance": entry["balance_after"],
        "memo": entry["memo"],
        "source": entry["source"],
    }


async def balance(conn, run_id, owner_id) -> int:
    """Return the owner's current balance."""
    return (await _WORLD._account(conn, run_id, owner_id))["balance"]


async def adjust(
    conn, run_id, owner_id, delta, memo, operation_key, source, *, counts_as_spend=False
) -> dict:
    """Change one balance once. The same key and payload return the first receipt.

    A reused key with a changed payload raises WorldError 409. A change that would take
    the balance below zero raises WorldError 409 and leaves the account unchanged. The
    world_changed event goes only to this owner. World effects from the Director or a
    site use this function, so money never moves through prose.
    """
    _operation_key(operation_key)
    if type(delta) is not int or delta == 0 or abs(delta) > MAX_AMOUNT:
        raise SiteError(f"A change must be a nonzero integer up to {MAX_AMOUNT}.", 422)
    memo = memo.strip() if isinstance(memo, str) else ""
    if not 1 <= len(memo) <= 500:
        raise SiteError("A memo of 1 to 500 characters is required.", 422)
    if not isinstance(source, str) or not 1 <= len(source) <= 80:
        raise SiteError("A source of 1 to 80 characters is required.", 422)
    if counts_as_spend and delta > 0:
        raise SiteError("Only a debit can count as online spending.", 422)
    payload_hash = _hash(
        {
            "owner_id": owner_id,
            "delta": delta,
            "memo": memo,
            "source": source,
            "spend": bool(counts_as_spend),
        }
    )
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        prior = await _one(
            conn,
            "SELECT * FROM wallet_ledger WHERE run_id=%s AND operation_key=%s",
            (run_id, operation_key),
        )
        if prior is not None:
            if prior["payload_hash"] != payload_hash:
                raise SiteError("The operation key was used with a different change.")
            return _receipt(prior)
        _WORLD._active(run)
        account = await _WORLD._account(conn, run_id, owner_id)
        after = account["balance"] + delta
        if after < 0:
            raise SiteError("The balance is too low for this change.")
        await conn.execute(
            "UPDATE world_accounts SET balance=%s WHERE run_id=%s AND owner_id=%s",
            (after, run_id, owner_id),
        )
        entry = await _one(
            conn,
            """INSERT INTO wallet_ledger(run_id,owner_id,operation_key,delta,balance_after,memo,
                      source,spend,payload_hash,sim_s)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                run_id,
                owner_id,
                operation_key,
                delta,
                after,
                memo,
                source,
                -delta if counts_as_spend else 0,
                payload_hash,
                await sim_now(conn, run_id),
            ),
        )
        await _WORLD._journal(conn, run_id, "wallet_adjust", owner_id)
        # The ledger keeps the source. Assistants can read this payload as a notice.
        await record_event(
            conn,
            run_id,
            "world_changed",
            run["current_window"],
            {"kind": "balance_changed", "delta": delta, "balance": after, "memo": memo},
            f"wallet:{operation_key}",
            owner_id,
        )
        return _receipt(entry)


async def approve(conn, run_id, owner_id, amount, operation_key) -> dict:
    """Add an owner's approval to what their assistant may spend online, once per key."""
    _operation_key(operation_key)
    _amount(amount, "An approval")
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        await _WORLD._account(conn, run_id, owner_id)
        prior = await _one(
            conn,
            "SELECT owner_id,amount FROM wallet_approvals WHERE run_id=%s AND operation_key=%s",
            (run_id, operation_key),
        )
        if prior is not None:
            if prior["owner_id"] != owner_id or prior["amount"] != amount:
                raise SiteError("The operation key was used with a different approval.")
            return await limits(conn, run_id, owner_id, run=run)
        _WORLD._active(run)
        await conn.execute(
            """INSERT INTO wallet_approvals(run_id,owner_id,operation_key,amount,sim_s)
               VALUES (%s,%s,%s,%s,%s)""",
            (run_id, owner_id, operation_key, amount, await sim_now(conn, run_id)),
        )
        await record_event(
            conn,
            run_id,
            "web_spend_approved",
            run["current_window"],
            {"amount": amount},
            f"wallet-approval:{operation_key}",
            owner_id,
        )
        return await limits(conn, run_id, owner_id, run=run)


async def limits(conn, run_id, owner_id, *, run=None) -> dict:
    """Return allowance + approved - spent. A refund restores money, not spending room."""
    if run is None:
        run = await _one(conn, "SELECT scenario FROM environment_runs WHERE id=%s", (run_id,))
        if run is None:
            raise SiteError("Run not found.", 404)
    allowance = (clock_section(run).get("online_allowance") or {}).get(owner_id, 0)
    totals = await _one(
        conn,
        """SELECT
           (SELECT COALESCE(sum(amount),0)::bigint FROM wallet_approvals
            WHERE run_id=%s AND owner_id=%s) AS approved,
           (SELECT COALESCE(sum(spend),0)::bigint FROM wallet_ledger
            WHERE run_id=%s AND owner_id=%s) AS spent""",
        (run_id, owner_id, run_id, owner_id),
    )
    return {
        "allowance": allowance,
        "approved": totals["approved"],
        "spent": totals["spent"],
        "remaining": max(0, allowance + totals["approved"] - totals["spent"]),
    }


async def spend(conn, run_id, owner_id, amount, memo, operation_key, source) -> dict:
    """Debit a card payment inside the approved limit, or return a typed decline.

    Result status is accepted or declined. Reason is approval_required or
    insufficient_funds. A decline changes nothing. The caller saves the decline event.
    """
    if type(amount) is not int or amount < 0:
        raise SiteError("An amount must be a whole number.", 422)
    if amount == 0:
        return {"status": "accepted", "delta": 0}
    async with conn.transaction():
        run = await _WORLD._run(conn, run_id)
        prior = await _one(
            conn,
            "SELECT 1 FROM wallet_ledger WHERE run_id=%s AND operation_key=%s",
            (run_id, operation_key),
        )
        if prior is None:
            current = await limits(conn, run_id, owner_id, run=run)
            if amount > current["remaining"]:
                return {
                    "status": "declined",
                    "reason": "approval_required",
                    "remaining": current["remaining"],
                }
            if amount > await balance(conn, run_id, owner_id):
                return {
                    "status": "declined",
                    "reason": "insufficient_funds",
                    "remaining": current["remaining"],
                }
        return await adjust(
            conn, run_id, owner_id, -amount, memo, operation_key, source, counts_as_spend=True
        )
