"""Supplied decisions for browser/runtime checks, never model-quality evidence."""

import json
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agentrpg.shop_demo import PRODUCTS


class ShopControlModel(BaseChatModel):
    client: Any
    run_id: str
    owner: str
    rounds: int
    calls: int = 0

    @property
    def _llm_type(self):
        return "supplied-shop-control"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, **kwargs):
        self.calls += 1
        # A new activation has its own final HumanMessage. Never use another
        # activation's browser controls or report as the current observation.
        start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        tools = [m for m in messages[start + 1 :] if isinstance(m, ToolMessage)]
        snapshot = next(
            (
                json.loads(m.content)["data"]
                for m in reversed(tools)
                if m.name in {"open_shop", "inspect_shop_page", "shop_click", "shop_fill"}
                and json.loads(m.content).get("ok")
            ),
            None,
        )

        def result(name, args):
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": name,
                                    "args": args,
                                    "id": str(uuid4()),
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            )

        async def get(suffix):
            response = await self.client.get(f"runs/{self.run_id}/{suffix}")
            response.raise_for_status()
            return response.json()

        if self.owner == "bob":
            market = await get("market")
            if not market["listings"]:
                calls = [
                    {
                        "name": "submit_market_action",
                        "id": str(uuid4()),
                        "type": "tool_call",
                        "args": {
                            "kind": "sell",
                            "resource_id": sku,
                            "quantity": self.rounds + 2,
                            "unit_price": price,
                        },
                    }
                    for sku, _, price, _ in PRODUCTS
                ]
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
                )
            return result("finish_activation", {"status": "finished", "text": ""})

        state = await get("shop/state")
        opened = [t for t in state["tasks"] if t["status"] == "open"]
        if opened:
            task = sorted(opened, key=lambda t: t["created_at"])[0]
            if snapshot is None:
                return result("open_shop", {"view": "catalog"})
            controls = {c["target"]: c for c in snapshot["controls"] if not c["disabled"]}
            if state["cart"]["lines"]:
                target = "place-order" if "place-order" in controls else "review-cart"
                return result("shop_click", {"target": target})
            listing = next(p for p in state["catalog"] if p["origin_id"] in task["item_ids"])
            if controls.get("shopping-task", {}).get("value") != task["id"]:
                return result("shop_fill", {"target": "shopping-task", "value": task["id"]})
            return result("shop_click", {"target": f"add-{listing['id']}"})

        accepted = [o for o in state["orders"] if o["status"] == "accepted"]
        if accepted:
            conversation = await get("owner/messages")
            reported = json.dumps(conversation, ensure_ascii=False)
            missing = [o for o in accepted if o["id"] not in reported]
            if missing:
                if snapshot is None:
                    return result("open_shop", {"view": "orders"})
                order = missing[0]
                return result(
                    "send_owner_message",
                    {
                        "content": f"订单已结算，共 {order['paid_total']} 模拟积分。"
                        f"订单：{order['id']}。"
                    },
                )
        return result("finish_activation", {"status": "finished", "text": ""})
