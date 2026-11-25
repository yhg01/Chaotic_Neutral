"""Evidence shared by the sealed-bid, routing and disclosure readers."""

from try_clock_research_group import only


def tools_for(trace, owner, name):
    return [
        e
        for e in trace["events"]
        if e["kind"] == "assistant_tool_call"
        and e["owner_id"] == owner
        and e["payload"]["tool"] == name
        and e["payload"]["result"].get("ok")
    ]


def mail_chain(t, message, *, sent=False):
    mid, who = message["id"], message["recipient_owner_id"]
    delivery = only(
        (
            e
            for e in t["events"]
            if e["kind"] == "inbox_received"
            and e["owner_id"] == who
            and e["payload"]["message_id"] == mid
        ),
        "delivery",
    )
    reads = [
        e for e in tools_for(t, who, "read_mail") if e["payload"]["result"]["data"]["id"] == mid
    ]
    assert reads and all(reads[0]["payload"]["result"]["data"][k] == v for k, v in message.items())
    ack = next(
        e
        for e in tools_for(t, who, "acknowledge_mail")
        if mid in e["payload"]["result"]["data"]["acknowledged_ids"]
        and mid in e["payload"]["arguments"]["message_ids"]
    )
    only(
        (
            r
            for r in t["receipts"]
            if r["owner_id"] == who and r["message_id"] == mid and r["read_at"]
        ),
        "persisted receipt",
    )
    assert delivery["created_at"] <= reads[0]["created_at"] <= ack["created_at"]
    chain = {"delivery": delivery, "read": reads[0], "ack": ack}
    if sent:
        send = only(
            (
                e
                for e in tools_for(t, message["sender_owner_id"], "send_mail")
                if e["payload"]["result"]["data"]["id"] == mid
            ),
            "committed send",
        )
        assert all(send["payload"]["result"]["data"][k] == v for k, v in message.items())
        chain["send"] = send
    return chain


def healthy(t):
    assert t["status"] == "completed" and all(j["status"] == "completed" for j in t["jobs"])
    assert not any(
        e["kind"]
        in {"job_failure", "simulator_error", "activation_abandoned", "director_effect_failed"}
        for e in t["events"]
    )
