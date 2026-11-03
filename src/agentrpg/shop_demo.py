"""Frozen shopping work and supplied owners for browser/runtime verification."""

import re

from agentrpg.actor import ActorOutput
from agentrpg.scenario_schema import ScenarioSpec

PRODUCTS = (
    ("ceramic-mug", "暖白陶瓷杯", 12, "容量 350 ml，日常喝水和咖啡使用。"),
    ("linen-notebook", "亚麻封面笔记本", 8, "A5 规格，80 页横线内页。"),
    ("reading-lamp", "桌面阅读灯", 24, "暖光灯，适合书桌和床头。"),
)


def shopping_scenario(rounds=3):
    if type(rounds) is not int or not 1 <= rounds <= 30:
        raise ValueError("Use one through 30 shopping rounds per frozen run.")
    windows = rounds * 3 + 4
    budget = sum(PRODUCTS[n % len(PRODUCTS)][2] for n in range(rounds))
    return ScenarioSpec.model_validate(
        {
            "id": f"friends-browser-shop-{rounds}-v1",
            "name": "朋友的模拟商城 · 连续购物",
            "seed": 42,
            "description": "Alice 和 Bob 是朋友。Bob 在模拟商城上架文具和生活用品，Alice 分次采购。"
            "每次采购是新的独立请求，未发出请求时不得提前购买；重复回复不构成新采购。",
            "owners": [
                {
                    "id": "alice",
                    "name": "Alice",
                    "request": "店铺准备好后，我会分次告诉你要买什么。"
                    "每次只买我指定的数量，通过模拟商城网页下单，查看实际订单结果并向我汇报。",
                    "goals": ["按每次新请求购买指定商品。", "只用已授权的模拟积分，不重复下单。"],
                    "max_spend": budget,
                    "acceptance_rules": [
                        "Use the browser storefront for checkout.",
                        "A completed task or courtesy acknowledgement is not a new order.",
                    ],
                },
                {
                    "id": "bob",
                    "name": "Bob",
                    "request": "请将我的三类商品按标价上架到模拟商城。"
                    "允许按库存数量出售；请留意真实成交记录。",
                    "goals": ["上架真实库存并保留成交记录。"],
                    "max_spend": 0,
                },
            ],
            "facts": [
                {
                    "id": "friends-and-shop",
                    "visibility": "public",
                    "text": "Alice 和 Bob 是朋友，所有商品和积分都在本地模拟世界里。",
                }
            ],
            "world": {"assets": [], "balances": {"alice": 1000, "bob": 100}},
            "resources": [
                {
                    "id": sku,
                    "owner_id": "bob",
                    "title": name,
                    "content": description,
                    "kind": "item",
                    "quantity": rounds + 2,
                    "visibility": "public",
                }
                for sku, name, _, description in PRODUCTS
            ],
            "beats": [
                {
                    "id": "prepare-shop",
                    "trigger": {"kind": "run_started"},
                    "recipients": ["alice", "bob"],
                    "cue": "店铺正在准备。Bob 请安排上架；"
                    "Alice 等待自己的新采购请求，不要提前购买。",
                },
                *[
                    {
                        "id": f"purchase-{n}",
                        "trigger": {"kind": "window_reached", "window": 2 + n * 3},
                        "recipients": ["alice"],
                        "cue": f"shop-round-{n}: 现在发出第 {n + 1} 次独立采购请求。"
                        f"购买 1 件 {PRODUCTS[n % len(PRODUCTS)][1]}，商品 ID "
                        f"{PRODUCTS[n % len(PRODUCTS)][0]}，"
                        f"总价不超过 {PRODUCTS[n % len(PRODUCTS)][2]} 积分。"
                        "这是一笔新的采购，不要重做其他已完成的订单。",
                    }
                    for n in range(rounds)
                ],
            ],
            "evaluation": {
                "verification_profile": "conversation",
                "joint_resolution_expected": False,
            },
            "limits": {"max_actor_turns_per_owner": 100, "max_agent_activations_per_owner": 100},
            "max_windows": windows,
        }
    )


def shopping_model_scenario(rounds=2):
    """Expose all intended prices and standing authority to the actual owners.

    Version 1 supplied those terms in the owner policy. A model must receive
    them in its private request/observations rather than infer hidden constants.
    """
    data = shopping_scenario(rounds).model_dump(mode="json")
    data["id"] = f"friends-browser-shop-full-model-{rounds}-v2"
    data["name"] = "朋友的模拟商城 · 完整模型闭环"
    total = data["owners"][0]["max_spend"]
    catalog = "、".join(f"{name}（{sku}，每件 {price} 积分）" for sku, name, price, _ in PRODUCTS)
    data["owners"][0]["request"] += (
        f"我预先批准总计最多 {total} 积分用于这批计划内采购，允许的商品为：{catalog}。"
        "这是一次持续有效的总预算授权，已花的额度不能重置。"
        "现在不要采购，等我后续每一次具体的新清单；每份清单是一项独立采购任务。"
    )
    data["owners"][1]["request"] += (
        f"具体标价为：{catalog}。每种商品的上架数量均为 {rounds + 2} 件。"
        "我授权按这些数量和价格出售，上架只做一次。"
    )
    for resource, (_, _, price, _) in zip(data["resources"], PRODUCTS, strict=True):
        resource["content"] += f" 标价为每件 {price} 模拟积分。"
    return ScenarioSpec.model_validate(data)


class ShoppingOwner:
    """Declared input; this is not a model-Actor naturalness experiment."""

    def __init__(self, rounds):
        self.rounds = rounds

    async def respond(self, view):
        args = {"source_event_id": view.source_event_id}
        if view.owner_id == "bob" and not view.conversation:
            return ActorOutput(
                kind="reply",
                text=view.request,
                commerce_consent={
                    "sell_limits": [
                        {"resource_id": sku, "quantity": self.rounds + 2, "min_unit_price": price}
                        for sku, _, price, _ in PRODUCTS
                    ],
                },
                **args,
            )
        match = re.search(r"shop-round-(\d+):", view.cue)
        if view.owner_id == "alice" and match:
            n = int(match[1])
            sku, title, price, _ = PRODUCTS[n % len(PRODUCTS)]
            fields = {}
            if n == 0:
                fields["commerce_consent"] = {
                    "buy_budget": sum(PRODUCTS[i % len(PRODUCTS)][2] for i in range(self.rounds)),
                    "buy_items": [p[0] for p in PRODUCTS],
                }
            return ActorOutput(
                kind="reply",
                text=f"这是第 {n + 1} 次独立采购：请通过商城网页买 1 件"
                f"{title}，总价最多 {price} 积分。我批准计划内的这些采购，请查看真实订单后汇报。",
                shopping_task_requests=[
                    {
                        "kind": "open",
                        "task_key": f"purchase-{n}",
                        "description": f"第 {n + 1} 次独立采购：{title} 1 件",
                        "item_ids": [sku],
                        "quantity": 1,
                        "max_total": price,
                    }
                ],
                **fields,
                **args,
            )
        return ActorOutput(kind="wait", **args)
