"""Wexley Council Permits: apply for a parking permit, see your permits, cancel one."""

ACTIONS = {
    "apply_permit": {
        "label": "Apply for a permit",
        "fields": {
            "plate": {
                "type": "text",
                "required": True,
                "max": 12,
                "hint": "5 to 8 letters and digits",
            },
            "zone": {"type": "text", "required": True, "max": 4},
            "kind": {"type": "choice", "required": True, "choices": ["resident", "business"]},
        },
    },
    "cancel_permit": {
        "label": "Cancel a permit",
        "fields": {"reference": {"type": "text", "required": True, "max": 12}},
    },
}
NAV = [{"label": "Your permits", "path": "/permits"}]


def permit_line(permit):
    return (
        f"- {permit['reference']} | {permit['plate']} | zone {permit['zone']} | "
        f"{permit['kind']} | ${permit['fee']} | {permit['status']}"
    )


def page(view, path, query):
    public = view["public"]
    fees = public.get("fees", {})
    if path == "/":
        return {
            "title": "Home",
            "sections": [
                {
                    "heading": "Parking permits",
                    "lines": [
                        "Zones: " + ", ".join(public.get("zones", [])),
                        f"Resident permit: ${fees.get('resident', 0)}",
                        f"Business permit: ${fees.get('business', 0)}",
                        "One vehicle can hold one active permit in a zone.",
                    ],
                    "forms": [{"action": "apply_permit", "path": "/"}],
                }
            ],
        }
    if path == "/permits":
        if not view["viewer"]:
            lines = ["Sign in to see your permits."]
            return {"title": "Your permits", "sections": [{"lines": lines}]}
        permits = view["account"].get("permits", [])
        forms = []
        if any(permit["status"] == "active" for permit in permits):
            forms.append({"action": "cancel_permit", "path": "/permits"})
        return {
            "title": "Your permits",
            "sections": [
                {
                    "heading": "Your permits",
                    "lines": [permit_line(permit) for permit in permits]
                    or ["You have no permits."],
                    "forms": forms,
                }
            ],
        }
    return None


def refuse(path, notice):
    return {"outcome": "rejected", "path": path, "notice": notice}


def act(view, action, fields):
    public = view["public"]
    account = view["account"]
    permits = account.get("permits", [])
    active = public.get("active", {})
    if action == "apply_permit":
        plate = fields["plate"].replace(" ", "").upper()
        if not 5 <= len(plate) <= 8 or not plate.isalnum():
            return refuse("/", "A plate has 5 to 8 letters and digits.")
        zone = fields["zone"].upper()
        if zone not in public.get("zones", []):
            return refuse("/", "We have no zone with that name.")
        key = f"{zone}:{plate}"
        if key in active:
            return refuse("/", f"That vehicle already has an active permit in zone {zone}.")
        fee = public.get("fees", {}).get(fields["kind"], 0)
        number = public.get("next_number", 1)
        reference = "WP-" + str(number).zfill(4)
        permits.append(
            {
                "reference": reference,
                "plate": plate,
                "zone": zone,
                "kind": fields["kind"],
                "fee": fee,
                "status": "active",
            }
        )
        account["permits"] = permits
        active[key] = reference
        public["active"] = active
        public["next_number"] = number + 1
        return {
            "path": "/permits",
            "notice": f"Permit {reference} is issued for {plate} in zone {zone}. We charged ${fee}.",
            "account": account,
            "public": public,
            "effects": [{"type": "charge", "amount": fee, "memo": f"Parking permit {reference}"}],
        }
    reference = fields["reference"].upper()
    for permit in permits:
        if permit["reference"] == reference and permit["status"] == "active":
            permit["status"] = "cancelled"
            active.pop(f"{permit['zone']}:{permit['plate']}", None)
            account["permits"] = permits
            public["active"] = active
            return {
                "path": "/permits",
                "notice": f"Permit {reference} is cancelled. There is no refund.",
                "account": account,
                "public": public,
            }
    return refuse("/permits", "We can't find an active permit with that reference on your account.")
