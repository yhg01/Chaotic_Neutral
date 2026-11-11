"""Wexley Land Register: a public index of titles and paid official copies."""

ACTIONS = {
    "search_titles": {
        "label": "Search the index",
        "write": False,
        "fields": {"q": {"type": "text", "required": True, "max": 80, "hint": "words of an address"}},
    },
    "buy_copy": {
        "label": "Order an official copy",
        "fields": {"title_number": {"type": "text", "required": True, "max": 12}},
    },
    "ask_registry": {
        "label": "Ask the registry",
        "fields": {
            "title_number": {"type": "text", "required": True, "max": 12},
            "message": {"type": "text", "required": True, "max": 2000},
        },
    },
}
NAV = [
    {"label": "Search titles", "path": "/titles"},
    {"label": "Your documents", "path": "/copies"},
]


def page(view, path, query):
    public = view["public"]
    titles = public.get("titles", {})
    copies = view["account"].get("copies", {})
    fee = public.get("fee", 0)
    if path == "/":
        return {
            "title": "Home",
            "sections": [
                {
                    "lines": [
                        "Search the index of titles by address. The index is free.",
                        f"An official copy shows the registered owner and charges. It costs ${fee}.",
                    ],
                    "forms": [{"action": "search_titles", "path": "/titles"}],
                }
            ],
        }
    if path == "/titles":
        words = str(query.get("q", "")).lower().split()
        found = [
            f"- {number} | {title['address']} | {title['tenure']}"
            for number, title in sorted(titles.items())
            if all(word in title["address"].lower() for word in words)
        ]
        return {
            "title": "Index of titles",
            "sections": [
                {
                    "heading": "Results" if words else "All titles",
                    "lines": found or ["No title matches those words."],
                    "forms": [
                        {"action": "search_titles", "path": "/titles"},
                        {"action": "buy_copy", "path": "/titles"},
                    ],
                }
            ],
        }
    if path == "/copies":
        if not view["viewer"]:
            return {"title": "Your documents", "sections": [{"lines": ["Sign in to see them."]}]}
        return {
            "title": "Your documents",
            "sections": [
                {
                    "heading": "Official copies",
                    "lines": [
                        f"- {number} | {held['address']} | issued {held['issued']}"
                        for number, held in sorted(copies.items())
                    ]
                    or ["You hold no official copies."],
                    "links": [
                        {"label": f"Official copy of {number}", "path": f"/copies/{number}"}
                        for number in sorted(copies)
                    ],
                }
            ],
        }
    if path.startswith("/copies/"):
        number = path.removeprefix("/copies/")
        held = copies.get(number)
        if held is None:
            return None
        return {
            "title": f"Official copy of {number}",
            "sections": [
                {
                    "lines": [
                        f"Title number: {number}",
                        f"Address: {held['address']}",
                        f"Tenure: {held['tenure']}",
                        f"Registered owner: {held['owner']}",
                        f"Issued: {held['issued']}",
                    ]
                },
                {
                    "heading": "Charges",
                    "lines": [f"- {charge}" for charge in held["charges"]]
                    or ["No charges are registered."],
                },
            ],
        }
    return None


def refuse(path, notice):
    return {"outcome": "rejected", "path": path, "notice": notice}


def act(view, action, fields):
    public = view["public"]
    if action == "search_titles":
        return {"path": "/titles", "query": {"q": fields["q"]}}
    number = fields["title_number"].replace(" ", "").upper()
    title = public.get("titles", {}).get(number)
    if title is None:
        return refuse("/titles", "We can't find that title number in the index.")
    if action == "ask_registry":
        return {
            "path": "/",
            "notice": "Thank you. The registry has your message.",
            "effects": [
                {
                    "type": "message",
                    "subject": f"Question about title {number}",
                    "body": fields["message"],
                }
            ],
        }
    account = view["account"]
    copies = account.get("copies", {})
    if number in copies:
        return refuse(f"/copies/{number}", "You already hold an official copy of this title.")
    fee = public.get("fee", 0)
    copies[number] = {
        "address": title["address"],
        "tenure": title["tenure"],
        "owner": title["owner"],
        "charges": title.get("charges", []),
        "issued": view["date"] or "today",
    }
    account["copies"] = copies
    return {
        "path": f"/copies/{number}",
        "notice": f"Your official copy of {number} is ready. We charged ${fee}.",
        "account": account,
        "effects": [{"type": "charge", "amount": fee, "memo": f"Official copy of {number}"}],
    }
