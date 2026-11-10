"""Audited operator recovery for an observed Actor schema rejection only."""

from copy import deepcopy
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from agentrpg.director import DirectorError, emit, row
from agentrpg.director_clock import DirectorClock, lock_key
from agentrpg.simulation_clock import digest


async def recover_confirmation_schema(
    pool, run_id, job_id, *, source_after, carried_output_tokens, evidence_reference
):
    """Keep the world, input, prices, roles and all old outputs/costs; retry no unknown call.

    This is an operator maintenance function, with no Agent tool or HTTP route.
    Its immutable receipt supports repair after a DB commit but lost local response.
    """
    run_id, job_id = UUID(str(run_id)), UUID(str(job_id))
    if (
        len(source_after) != 64
        or any(c not in "0123456789abcdef" for c in source_after)
        or type(carried_output_tokens) is not int
        or carried_output_tokens < 0
        or not 1 <= len(evidence_reference) <= 240
    ):
        raise DirectorError("Invalid Actor schema recovery evidence.")
    identity = uuid5(job_id, "confirmation-schema-recovery:" + source_after)
    async with DirectorClock(pool)._locked(run_id) as (conn, binding, clock, run):
        prior = await row(
            conn, "SELECT * FROM clock_runtime_actor_rejections WHERE id=%s", (identity,)
        )
        if prior:
            if (
                prior["carried_output_tokens"] != carried_output_tokens
                or prior["evidence_reference"] != evidence_reference
            ):
                raise DirectorError("Recovery ID already has different evidence.")
            return prior
        lease = await row(
            conn, "SELECT pg_try_advisory_xact_lock(%s) AS ok", (lock_key(f"clock-job:{job_id}"),)
        )
        if not lease["ok"]:
            raise DirectorError("This Actor dispatch still has a live worker.")
        profile = await row(
            conn, "SELECT * FROM clock_runtime_profiles WHERE run_id=%s FOR UPDATE", (run_id,)
        )
        job = await row(
            conn,
            "SELECT * FROM director_clock_jobs WHERE run_id=%s AND id=%s FOR UPDATE",
            (run_id, job_id),
        )
        saved = await row(
            conn, "SELECT * FROM clock_runtime_actor_outputs WHERE job_id=%s FOR UPDATE", (job_id,)
        )
        if (
            not profile
            or not job
            or not saved
            or job["role"] != "actor"
            or job["status"] != "uncertain"
            or job["output"] is not None
            or job["owner_message_id"]
            or saved["timed_output"] is not None
            or saved["output"].get("failure_kind") != "ActorContractError"
            or job["input_hash"] != digest(job["input"])
        ):
            raise DirectorError("Only a captured, rejected Actor schema response is recoverable.")
        rejected = [x for x in saved["diagnostics"] if x.get("accepted") is False]
        measured = saved["output"].get("failed_output_tokens")
        if measured is not None and measured != carried_output_tokens:
            raise DirectorError("Recovery must retain the measured generation time.")
        reason = "Each current confirmation decision requires exactly one typed owner action."
        if not rejected or any(
            x.get("review") is not None
            or not x.get("generated_candidate")
            or x.get("generated_candidate", {}).get("confirmation_actions") != []
            or not any(i.get("message") == reason for i in x.get("issues", []))
            for x in rejected
        ):
            raise DirectorError(
                "The response is not the known missing-confirmation-field rejection."
            )
        if await row(
            conn,
            "SELECT 1 FROM model_spend_attempts WHERE run_id=%s AND status<>'reported' LIMIT 1",
            (run_id,),
        ):
            raise DirectorError("Unresolved provider attempts cannot be regenerated.")
        old = profile["configuration"]
        new = deepcopy(old)
        source_before = old["operator_configuration"]["source_hash"]
        if source_before == source_after:
            raise DirectorError("Recovery requires an explicit reviewed source transition.")
        new["operator_configuration"]["source_hash"] = source_after
        await conn.execute(
            "INSERT INTO clock_runtime_actor_rejections "
            "(id,run_id,job_id,source_before,source_after,input_hash,rejected_output,diagnostics,"
            "configuration_before,configuration_after,carried_output_tokens,evidence_reference) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                identity,
                run_id,
                job_id,
                source_before,
                source_after,
                job["input_hash"],
                Jsonb(saved["output"]),
                Jsonb(saved["diagnostics"]),
                Jsonb(old),
                Jsonb(new),
                carried_output_tokens,
                evidence_reference,
            ),
        )
        await conn.execute("DELETE FROM clock_runtime_actor_outputs WHERE job_id=%s", (job_id,))
        await conn.execute(
            "UPDATE director_clock_jobs SET status='pending',attempts=0,claim_token=NULL,"
            "error=NULL WHERE id=%s",
            (job_id,),
        )
        await conn.execute(
            "UPDATE clock_runtime_profiles SET configuration=%s,configuration_hash=%s "
            "WHERE run_id=%s",
            (Jsonb(new), digest(new), run_id),
        )
        await emit(
            conn,
            run_id,
            "clock_actor_schema_recovery",
            0,
            {
                "job_id": str(job_id),
                "source_before": source_before,
                "source_after": source_after,
                "input_hash": job["input_hash"],
                "rejection_diagnostics_hash": digest(saved["diagnostics"]),
                "carried_output_tokens": carried_output_tokens,
                "evidence_reference": evidence_reference,
            },
            "clock-schema-recovery:" + str(identity),
            job["owner_id"],
        )
        return await row(
            conn, "SELECT * FROM clock_runtime_actor_rejections WHERE id=%s", (identity,)
        )
