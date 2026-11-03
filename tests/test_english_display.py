"""Display translations cannot modify evidence or spend a campaign budget."""

import json
from copy import deepcopy

from starlette.requests import Request

from agentrpg.english_display import (
    EnglishDisplay,
    chinese_texts,
    english_view,
    marker,
    sentence_parts,
)


def test_collect_values_without_exposing_keys():
    assert chinese_texts({"秘密": "English", "items": ["已完成", "已完成", 12]}) == {"已完成"}
    assert len({marker(index) for index in range(200)}) == 200
    assert all(label.isalpha() for label in (marker(i) for i in range(200)))
    identifier = "12345678-1234-4321-9876-123456789012"
    long_source = "检查" * 85 + identifier + "余额" * 100 + "12345.6789" + "继续" * 100
    parts = list(sentence_parts(long_source))
    assert "".join(parts) == long_source
    assert any(identifier in part for part in parts)
    assert any("12345.6789" in part for part in parts)


def test_missing_marker_fallback_preserves_ids_amounts_and_sentences(tmp_path):
    calls = []

    def infer(texts):
        calls.extend(texts)
        return ["Translated phrase." for _ in texts]

    display = EnglishDisplay(tmp_path, infer=infer)
    source = "已支付 25.50 积分，订单 shop-order-123。不要再付第二次。"
    result = display.translations([source])[source]
    assert "25.50" in result and "shop-order-123" in result
    assert "credits" in result and not chinese_texts(result)
    assert result.count("Translated phrase.") >= 2
    count = len(calls)
    assert display.translations([source])[source] == result
    assert len(calls) == count
    cached = json.loads(display.path.read_text())
    assert source not in cached["texts"]
    assert display.path.stat().st_mode & 0o777 == 0o600


async def test_sidecar_is_opt_in_and_leaves_original_evidence_unchanged(tmp_path):
    from types import SimpleNamespace

    app = SimpleNamespace(
        state=SimpleNamespace(
            english_display=EnglishDisplay(
                tmp_path, infer=lambda texts: ["Transfer unsuccessful."] * len(texts)
            )
        )
    )
    original = {"chat": [{"content": "转账失败。", "id": "original-id"}]}
    saved = deepcopy(original)
    request = Request({"type": "http", "query_string": b"display=en", "app": app})
    result = await english_view(request, original)
    assert original == saved
    assert result["chat"] == saved["chat"]
    assert result["english"]["texts"]["转账失败。"] == "Transfer unsuccessful."
    request = Request({"type": "http", "query_string": b"", "app": app})
    assert await english_view(request, original) is original


async def test_missing_local_model_is_explicit(tmp_path):
    from types import SimpleNamespace

    app = SimpleNamespace(state=SimpleNamespace(console_root=tmp_path))
    request = Request({"type": "http", "query_string": b"display=en", "app": app})
    result = await english_view(request, {"chat": []})
    assert result["english"] == {"available": False, "texts": {}, "local": True}
