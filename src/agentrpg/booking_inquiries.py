"""Owner-scoped booking inquiries with scripted responses on the existing clock."""

from typing import Literal
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import Field, model_serializer, model_validator

from agentrpg.resource_tools import BookingOptionsRequest
from agentrpg.scenario_schema import Contract, Identifier, Text
from agentrpg.world import WorldError, WorldService, _hash, _one


class BookingOption(Contract):
    site_id: Identifier
    slot_id: Identifier


class ContactOptions(Contract):
    contact_id: Identifier
    delay_seconds: int = Field(strict=True, ge=1, le=86400)
    options: tuple[BookingOption, ...] = Field(min_length=1, max_length=20)


class BookingTask(Contract):
    id: Identifier
    owner_id: Identifier
    requirements: Text
    contacts: tuple[ContactOptions, ...] = Field(min_length=1, max_length=14)

    @model_validator(mode="after")
    def distinct_contacts(self):
        ids = [c.contact_id for c in self.contacts]
        if len(set(ids)) != len(ids) or self.owner_id in ids:
            raise ValueError("Booking contacts must be distinct peers.")
        return self


class BookingProfile(Contract):
    tasks: tuple[BookingTask, ...] = Field(min_length=1, max_length=100)
    response_source: Literal["scripted_contact", "peer_agent"] = "scripted_contact"

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        data = handler(self)
        if self.response_source == "scripted_contact":
            data.pop("response_source")
        return data

    @model_validator(mode="after")
    def distinct_tasks(self):
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("Meeting task IDs must be distinct.")
        return self


def receipt(record):
    # Never expose the response schedule, frozen options, or other owners' tasks.
    return {
        key: record[key] for key in ("id", "contact_id", "meeting_task_id", "status", "options")
    }


class BookingInquiryService:
    def __init__(self, pool):
        self.pool = pool
        self.world = WorldService(pool)

    async def configure(self, run_id, configuration):
        """Trusted provisioning only; immutable after start. No participant API."""
        profile = BookingProfile.model_validate(configuration)
        config = profile.model_dump(mode="json")
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            old = await _one(
                conn, "SELECT * FROM booking_inquiry_profiles WHERE run_id=%s", (run_id,)
            )
            if old:
                if old["configuration_hash"] != _hash(config):
                    raise WorldError("Booking configuration has changed.")
                return
            if run["status"] != "pending" or not run["scenario"].get("clock"):
                raise WorldError("Configure booking inquiries before starting a clocked run.")
            owners = {owner["id"] for owner in run["scenario"]["owners"]}
            sites = {site["site_id"]: site for site in run["scenario"]["clock"].get("sites", [])}
            for task in profile.tasks:
                if task.owner_id not in owners:
                    raise WorldError("Unknown task owner.", 422)
                for contact in task.contacts:
                    if contact.contact_id not in owners:
                        raise WorldError("Unknown booking contact.", 422)
                    for option in contact.options:
                        site = sites.get(option.site_id, {})
                        if option.slot_id not in {slot["id"] for slot in site.get("slots", [])}:
                            raise WorldError("Unknown booking slot.", 422)
            await conn.execute(
                "INSERT INTO booking_inquiry_profiles VALUES (%s,%s,%s)",
                (run_id, Jsonb(config), _hash(config)),
            )

    async def _contact(self, conn, run_id, owner_id, task_id, contact_id):
        profile = await _one(
            conn, "SELECT configuration FROM booking_inquiry_profiles WHERE run_id=%s", (run_id,)
        )
        if profile is None:
            raise WorldError("Booking inquiries are not enabled.", 404)
        task = next(
            (
                t
                for t in profile["configuration"]["tasks"]
                if t["id"] == task_id and t["owner_id"] == owner_id
            ),
            None,
        )
        if task is None:
            raise WorldError("Meeting task not found.", 404)
        contact = next((c for c in task["contacts"] if c["contact_id"] == contact_id), None)
        if contact is None:
            raise WorldError("Booking contact not permitted.", 403)
        return contact

    async def request(self, run_id, owner_id, request_id, arguments):
        from agentrpg.clock import schedule

        args = BookingOptionsRequest.model_validate(arguments)
        digest = _hash(args.model_dump(mode="json"))
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            prior = await _one(
                conn,
                "SELECT * FROM booking_inquiries WHERE run_id=%s AND owner_id=%s AND request_id=%s",
                (run_id, owner_id, request_id),
            )
            if prior:
                if prior["payload_hash"] != digest:
                    raise WorldError("The request ID was used with a different payload.")
                # Original write response remains stable even after delivery.
                return {**receipt(prior), "status": "pending", "options": None}
            self.world._active(run)
            profile = await _one(
                conn,
                "SELECT configuration FROM booking_inquiry_profiles WHERE run_id=%s",
                (run_id,),
            )
            if profile and profile["configuration"].get("response_source") == "peer_agent":
                raise WorldError("This profile requires a peer-written reply.", 403)
            contact = await self._contact(
                conn, run_id, owner_id, args.meeting_task_id, args.contact_id
            )
            clock = await _one(conn, "SELECT sim_s FROM clock_state WHERE run_id=%s", (run_id,))
            if clock is None:
                raise WorldError("The booking clock is not initialized.")
            due = clock["sim_s"] + contact["delay_seconds"]
            if due >= run["scenario"]["clock"]["duration_minutes"] * 60:
                raise WorldError("The contact cannot respond before closing time.")
            inquiry_id = uuid5(UUID(str(run_id)), f"booking-inquiry:{owner_id}:{request_id}")
            record = await _one(
                conn,
                "INSERT INTO booking_inquiries(run_id,id,owner_id,contact_id,meeting_task_id,"
                "request_id,payload_hash,due_s) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    run_id,
                    inquiry_id,
                    owner_id,
                    args.contact_id,
                    args.meeting_task_id,
                    request_id,
                    digest,
                    due,
                ),
            )
            await schedule(
                conn,
                run_id,
                due,
                "booking_options_reply",
                {"inquiry_id": str(inquiry_id)},
                f"booking-options:{inquiry_id}",
                owner_id,
            )
            await self.world._event(
                conn,
                run_id,
                run["current_window"],
                "booking_options_requested",
                owner_id,
                f"booking-request:{inquiry_id}",
                {
                    "inquiry_id": str(inquiry_id),
                    "contact_id": args.contact_id,
                    "meeting_task_id": args.meeting_task_id,
                    "response_source": "scripted_contact",
                },
            )
            return receipt(record)

    async def read(self, run_id, owner_id, inquiry_id):
        async with self.pool.connection() as conn:
            record = await _one(
                conn,
                "SELECT * FROM booking_inquiries WHERE run_id=%s AND owner_id=%s AND id=%s",
                (run_id, owner_id, inquiry_id),
            )
            if record is None:
                raise WorldError("Booking inquiry not found.", 404)
            return receipt(record)

    async def queue_reply(self, run_id, contact_id, request_id, thread_id, body):
        from agentrpg.clock import ClockConfig, event_time_s, schedule
        from agentrpg.schemas import NewReply

        payload = NewReply(request_id=request_id, body=body)
        digest = _hash({"thread_id": str(thread_id), "body": payload.body})
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            profile = await _one(
                conn,
                "SELECT configuration FROM booking_inquiry_profiles WHERE run_id=%s",
                (run_id,),
            )
            if not profile or profile["configuration"].get("response_source") != "peer_agent":
                raise WorldError("Peer replies are not enabled.", 403)
            root = await _one(
                conn,
                "SELECT m.*,p.owner_id FROM messages m JOIN run_participants p "
                "ON p.agent_id=m.author_id AND p.run_id=%s "
                "JOIN run_participants peer ON peer.run_id=p.run_id AND peer.owner_id=%s "
                "WHERE m.id=%s AND m.is_root AND m.audience=ARRAY[peer.agent_id]",
                (run_id, contact_id, thread_id),
            )
            if root is None:
                raise WorldError("Meeting thread not found.", 404)
            contact = await self._contact(conn, run_id, root["owner_id"], root["title"], contact_id)
            prior = await _one(
                conn,
                "SELECT * FROM booking_inquiries WHERE run_id=%s AND owner_id=%s AND request_id=%s",
                (run_id, root["owner_id"], request_id),
            )
            if prior:
                if prior["payload_hash"] != digest or prior["contact_id"] != contact_id:
                    raise WorldError("The request ID was used with a different payload.")
                inquiry_id = prior["id"]
            else:
                self.world._active(run)
                first = await _one(
                    conn,
                    "SELECT created_at FROM messages WHERE cluster_id=%s AND author_id=%s "
                    "AND title=%s AND audience=%s ORDER BY created_at,id LIMIT 1",
                    (run["cluster_id"], root["author_id"], root["title"], root["audience"]),
                )
                clock = await _one(
                    conn, "SELECT sim_s,pop_seq FROM clock_state WHERE run_id=%s", (run_id,)
                )
                config = ClockConfig.from_section(run["scenario"]["clock"], run["scenario"]["seed"])
                due = max(
                    clock["sim_s"],
                    event_time_s(first["created_at"], config) + contact["delay_seconds"],
                )
                if due >= config.duration_s:
                    raise WorldError("The reply cannot arrive before closing time.")
                inquiry_id = uuid5(UUID(str(run_id)), f"peer-reply:{contact_id}:{request_id}")
                await conn.execute(
                    "INSERT INTO booking_inquiries(run_id,id,owner_id,contact_id,meeting_task_id,"
                    "request_id,payload_hash,due_s,thread_id,response) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        inquiry_id,
                        root["owner_id"],
                        contact_id,
                        root["title"],
                        request_id,
                        digest,
                        due,
                        thread_id,
                        Jsonb(
                            {
                                "body": payload.body,
                                "prepared_s": clock["sim_s"],
                                "order": clock["pop_seq"],
                            }
                        ),
                    ),
                )
                await schedule(
                    conn,
                    run_id,
                    due,
                    "booking_options_reply",
                    {"inquiry_id": str(inquiry_id)},
                    f"peer-reply:{root['owner_id']}:{contact_id}:{root['title']}:{request_id}",
                    root["owner_id"],
                )
            return {
                "status": "pending",
                "inquiry_id": str(inquiry_id),
                "thread_id": str(thread_id),
                "detail": "Your reply is queued for delivery. A pending receipt is not delivery; "
                "do not resend it merely because it is pending.",
            }

    async def deliver(self, run_id, inquiry_id):
        """Called by the clock. Response and exact-recipient event commit together."""
        async with self.pool.connection() as conn, conn.transaction():
            run = await self.world._run(conn, run_id)
            self.world._active(run)
            record = await _one(
                conn,
                "SELECT * FROM booking_inquiries WHERE run_id=%s AND id=%s FOR UPDATE",
                (run_id, inquiry_id),
            )
            if record is None:
                raise WorldError("Booking inquiry not found.", 404)
            if record["status"] == "ready":
                return receipt(record)
            clock = await _one(conn, "SELECT sim_s FROM clock_state WHERE run_id=%s", (run_id,))
            if clock is None or clock["sim_s"] < record["due_s"]:
                raise WorldError("Booking options are not ready.")
            if record["response"] is not None:
                from agentrpg.main import Principal, write_message_in_transaction
                from agentrpg.schemas import NewReply

                peer = await _one(
                    conn,
                    "SELECT a.id,a.cluster_id,a.name FROM agents a JOIN run_participants p "
                    "ON p.agent_id=a.id WHERE p.run_id=%s AND p.owner_id=%s",
                    (run_id, record["contact_id"]),
                )
                pending = await (
                    await conn.execute(
                        "SELECT * FROM booking_inquiries WHERE run_id=%s AND owner_id=%s "
                        "AND contact_id=%s AND meeting_task_id=%s AND response IS NOT NULL "
                        "AND status='pending' AND due_s<=%s "
                        "ORDER BY (response->>'order')::bigint,id",
                        (
                            run_id,
                            record["owner_id"],
                            record["contact_id"],
                            record["meeting_task_id"],
                            clock["sim_s"],
                        ),
                    )
                ).fetchall()
                for answer in pending:
                    message, _ = await write_message_in_transaction(
                        conn,
                        Principal("agent", peer),
                        run["cluster_id"],
                        NewReply(request_id=answer["request_id"], body=answer["response"]["body"]),
                        answer["thread_id"],
                    )
                    await conn.execute(
                        "UPDATE booking_inquiries SET status='ready',options=%s "
                        "WHERE run_id=%s AND id=%s",
                        (Jsonb([{"message_id": str(message["id"])}]), run_id, answer["id"]),
                    )
                return receipt(
                    await _one(
                        conn,
                        "SELECT * FROM booking_inquiries WHERE run_id=%s AND id=%s",
                        (run_id, inquiry_id),
                    )
                )
            contact = await self._contact(
                conn, run_id, record["owner_id"], record["meeting_task_id"], record["contact_id"]
            )
            record = await _one(
                conn,
                "UPDATE booking_inquiries SET status='ready',options=%s "
                "WHERE run_id=%s AND id=%s RETURNING *",
                (Jsonb(contact["options"]), run_id, inquiry_id),
            )
            await self.world._event(
                conn,
                run_id,
                run["current_window"],
                "booking_options_ready",
                record["owner_id"],
                f"booking-ready:{inquiry_id}",
                {
                    "inquiry_id": str(inquiry_id),
                    "contact_id": record["contact_id"],
                    "meeting_task_id": record["meeting_task_id"],
                },
            )
            return receipt(record)
