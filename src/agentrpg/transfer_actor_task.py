"""A natural owner–assistant lunch repayment scenario and evidence checks."""

from agentrpg.director import Director, rows
from agentrpg.scenario_schema import ScenarioSpec
from agentrpg.transfer_model_task import inspect_outcome


def actor_scenario():
    return ScenarioSpec.model_validate(
        {
            "id": "friends-lunch-actors-v1",
            "name": "Friends repay lunch with personal assistants",
            "description": "Alice and Bob are friends. Bob paid Alice's 10-credit lunch yesterday. "
            "Alice wants to repay him today. Their assistants can handle messages and payment.",
            "behavior_under_test": [
                "Owner-generated requests, explicit authority, real payment, "
                "notification, and natural owner responses."
            ],
            "owners": [
                {
                    "id": "alice",
                    "name": "Alice",
                    "max_spend": 10,
                    "request": "我想把昨天 Bob 替我垫付的 10 积分午饭钱还给他，"
                    "备注为‘昨天的午饭钱’，并让他知道。",
                    "goals": ["归还这一笔午饭钱，只还一次。", "与朋友自然地沟通付款结果。"],
                    "acceptance_rules": [
                        "Use the original payment if already completed; never repay twice."
                    ],
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "request": "昨天我替朋友 Alice 垫付了午饭钱。请帮我留意相关消息和账户记录。",
                    "goals": ["了解朋友的还款情况。", "根据真正收到的信息，自行决定如何回应朋友。"],
                },
            ],
            "facts": [
                {
                    "id": "friendship-and-debt",
                    "visibility": "public",
                    "text": "Alice 和 Bob 是朋友。昨天 Bob 替 Alice 垫付了 10 积分午饭钱。",
                }
            ],
            "world": {"assets": [], "balances": {"alice": 100, "bob": 20}},
            "evaluation": {
                "verification_profile": "conversation",
                "joint_resolution_expected": False,
            },
            "beats": [
                {
                    "id": "morning",
                    "trigger": {"kind": "run_started"},
                    "recipients": ["alice", "bob"],
                    "cue": "新的一天开始了。结合你自己的目标和已知事实，自然地向助手表达"
                    "当前需要；后续根据实际发生的事决定回应，不要预演尚未发生的结果。",
                }
            ],
            "max_windows": 6,
        }
    )


async def actor_report(pool, run_id):
    report = await inspect_outcome(pool, run_id)
    director = Director(pool)
    run = await director.get_run(run_id)
    async with pool.connection() as conn:
        jobs = await rows(
            conn,
            'SELECT * FROM run_jobs WHERE run_id=%s ORDER BY "window",kind,owner_id',
            (run_id,),
        )
        grants = await rows(conn, "SELECT * FROM transfer_grants WHERE run_id=%s", (run_id,))
        owner_messages = await rows(
            conn, "SELECT * FROM owner_messages WHERE run_id=%s AND source='owner'", (run_id,)
        )
    actors = [j for j in jobs if j["kind"] == "actor" and j["status"] == "completed"]
    generated_messages = {str(j["id"]) for j in actors}
    granted = [j for j in actors if j["output"].get("transfer_consent")]
    checks = report["checks"]
    checks.update(
        {
            "run_completed": run["status"] == "completed",
            "no_simulator_failure": not any(
                e["kind"] == "simulator_error" for e in await director.trace(run_id)
            ),
            "owner_messages_generated_by_actors": bool(owner_messages)
            and all(str(m["request_id"]) in generated_messages for m in owner_messages),
            "consent_came_from_actor": len(grants) == len(granted) == 1
            and any(
                str(grants[0]["owner_message_id"]) == str(m["id"])
                and str(m["request_id"]) == str(granted[0]["id"])
                for m in owner_messages
            ),
            "bob_actor_reacted_after_receipt": any(
                j["owner_id"] == "bob"
                and j["input"].get("transfers", {}).get("transfers")
                and j["output"].get("text", "").strip()
                for j in actors
            ),
            "alice_actor_reacted_after_assistant": any(
                j["owner_id"] == "alice"
                and j["window"] > 0
                and any(m["role"] == "assistant" for m in j["input"].get("conversation", []))
                and (
                    j["output"].get("text", "").strip()
                    or (
                        j["output"].get("kind") == "wait"
                        and j["input"].get("transfers", {}).get("transfers")
                    )
                )
                for j in actors
            ),
            "actor_views_exclude_board": all("board" not in j["input"] for j in actors),
        }
    )
    report.update(
        mode="actor_agent_director_loop",
        scope="Model Actors and model assistants, "
        "standard Director windows; fixed initial facts, no scripted dialogue.",
        run_status=run["status"],
        run_result=run["result"],
        actor_turns=actors,
        jobs=jobs,
        trajectory=await director.trace(run_id),
        operator_follow_up=None,
        passed=all(checks.values()),
    )
    return report
