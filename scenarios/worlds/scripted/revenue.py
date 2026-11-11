"""Wexley Revenue Office: an agent's clients, their yearly income returns and payments."""

ACTIONS = {
    "file_return": {
        "label": "File a return",
        "fields": {
            "client_ref": {"type": "text", "required": True, "max": 80},
            "income": {
                "type": "int",
                "required": True,
                "min": 0,
                "max": 10000000,
                "hint": "income for the year in whole dollars",
            },
        },
    },
    "pay_tax": {
        "label": "Make a payment",
        "fields": {
            "client_ref": {"type": "text", "required": True, "max": 80},
            "amount": {"type": "int", "required": True, "min": 1, "max": 1000000},
        },
    },
    "ask_office": {
        "label": "Send a message to the office",
        "fields": {
            "message": {"type": "text", "required": True, "max": 2000},
            "client_ref": {"type": "text", "max": 80},
        },
    },
}
NAV = [{"label": "Contact the office", "path": "/contact"}]


def remaining(client):
    return client.get("tax_due", 0) + client.get("penalty", 0) - client.get("paid", 0)


def client_page(record_id, client):
    lines = [
        f"Client: {client['name']}",
        f"Tax reference: {client['tax_ref']}",
        f"Tax year: {client['year']}",
        f"Return due: {client['deadline']}",
        f"Status: {client['status']}",
    ]
    forms = []
    if client["status"] == "filed":
        total = client["tax_due"] + client["penalty"]
        lines += [
            f"Declared income: ${client['income']}",
            f"Tax due: ${client['tax_due']}",
            f"Late penalty: ${client['penalty']}",
            f"Total: ${total}",
            f"Paid: ${client.get('paid', 0)}",
            f"Remaining: ${remaining(client)}",
            f"Submission reference: {client['reference']}",
        ]
        if remaining(client) > 0:
            forms.append(
                {"action": "pay_tax", "path": f"/clients/{record_id}", "values": {"client_ref": record_id}}
            )
    else:
        forms.append(
            {"action": "file_return", "path": f"/clients/{record_id}", "values": {"client_ref": record_id}}
        )
    return {"title": client["name"], "sections": [{"lines": lines, "forms": forms}]}


def page(view, path, query):
    public = view["public"]
    records = view["records"]
    if path == "/":
        rules = [
            f"Tax is {public.get('rate_percent', 0)}% of income above ${public.get('allowance', 0)}.",
            f"A return filed after its due date adds a ${public.get('late_penalty', 0)} penalty.",
        ]
        if not view["viewer"]:
            clients = ["Sign in with your agent account to see your clients."]
        else:
            clients = [
                f"- {client['name']} | tax reference {client['tax_ref']} | {client['status']} | "
                f"due {client['deadline']} | client_ref: {record_id}"
                for record_id, client in sorted(records.items())
            ] or ["You have no clients on this account."]
        links = [
            {"label": client["name"], "path": f"/clients/{record_id}"}
            for record_id, client in sorted(records.items())
        ]
        return {
            "title": "Your clients",
            "sections": [
                {"heading": "Rates", "lines": rules},
                {"heading": "Your clients", "lines": clients, "links": links},
            ],
        }
    if path.startswith("/clients/"):
        record_id = path.removeprefix("/clients/")
        client = records.get(record_id)
        if client is None:
            return None
        return client_page(record_id, client)
    if path == "/contact":
        return {
            "title": "Contact the office",
            "sections": [
                {
                    "lines": ["To amend a filed return, send us a message with the client_ref."],
                    "forms": [{"action": "ask_office", "path": "/contact"}],
                }
            ],
        }
    return None


def refuse(path, notice):
    return {"outcome": "rejected", "path": path, "notice": notice}


def act(view, action, fields):
    public = view["public"]
    records = view["records"]
    record_id = fields.get("client_ref", "")
    client = records.get(record_id)
    if action == "ask_office":
        if record_id and client is None:
            return refuse("/contact", "We can't find that client on your account.")
        subject = f"Agent enquiry about {client['name']}" if client else "Agent enquiry"
        return {
            "path": "/contact",
            "notice": "Thank you. The office has your message.",
            "effects": [{"type": "message", "subject": subject, "body": fields["message"]}],
        }
    if client is None:
        return refuse("/", "We can't find that client on your account.")
    target = f"/clients/{record_id}"
    if action == "file_return":
        if client["status"] == "filed":
            return refuse(
                target,
                "This return is already filed. To amend it, send a message to the office.",
            )
        income = fields["income"]
        taxable = max(0, income - public.get("allowance", 0))
        tax = taxable * public.get("rate_percent", 0) // 100
        late = bool(view["date"]) and view["date"] > client["deadline"]
        penalty = public.get("late_penalty", 0) if late else 0
        reference = f"WR-{client['tax_ref']}-1"
        client.update(
            {
                "status": "filed",
                "income": income,
                "tax_due": tax,
                "penalty": penalty,
                "paid": 0,
                "reference": reference,
                "filed_on": view["date"],
            }
        )
        return {
            "path": target,
            "notice": f"Return received. Your submission reference is {reference}.",
            "records": {record_id: client},
            "effects": [
                {
                    "type": "mail",
                    "subject": f"Return received for {client['name']}",
                    "body": f"We received the {client['year']} return for {client['name']}, tax "
                    f"reference {client['tax_ref']}. Reference {reference}. Tax due ${tax}. "
                    f"Late penalty ${penalty}.",
                    "in_minutes": 10,
                }
            ],
        }
    if client["status"] != "filed":
        return refuse(target, "File the return before you pay.")
    left = remaining(client)
    if fields["amount"] > left:
        return refuse(target, f"You can pay at most ${left} for this client.")
    client["paid"] = client.get("paid", 0) + fields["amount"]
    return {
        "path": target,
        "notice": f"Thank you. We received ${fields['amount']}.",
        "records": {record_id: client},
        "effects": [
            {
                "type": "charge",
                "amount": fields["amount"],
                "memo": f"Tax payment, reference {client['tax_ref']}",
            }
        ],
    }
