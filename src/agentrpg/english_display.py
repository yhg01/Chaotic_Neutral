"""Local, derived English reading aids. Saved evidence is never modified.

Optional CTranslate2 inference has no HTTP client, model-provider adapter or
campaign binding. Its rebuildable cache contains no operational credentials.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[2]
HAN = re.compile(r"[\u3400-\u9fff]")
PARTS = re.compile(r"[^。！？；\n]+[。！？；]?|\n+")
PROTECTED = re.compile(
    r"https?://[^\s，。；）)]+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"|\b[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+\b|\d+(?:\.\d+)?"
)
GLOSSARY = {
    "朋友的模拟商城 · 连续购物": "Friends' simulated store · Repeat purchases",
    "朋友的模拟商城 · 完整模型闭环": "Friends' simulated store · Model owners and assistants",
    "暖白陶瓷杯": "ceramic mug",
    "亚麻封面笔记本": "linen notebook",
    "桌面阅读灯": "reading lamp",
    "模拟商城": "simulated store",
    "模拟积分": "simulated credits",
    "积分": "credits",
    "轨迹": "trajectory",
    "只读": "Read only",
    "处理中": "Pending",
    "已结算": "Settled",
    "未成交": "Rejected",
    "已暂停": "Paused",
    "运行中": "Running",
}


def marker(index):
    # Alphabetic labels cannot be mistaken for an amount or a receipt number.
    label = ""
    while True:
        label = chr(65 + index % 26) + label
        index = index // 26 - 1
        if index < 0:
            return "ZXQ" + label + "ZXQ"


def sentence_parts(source):
    for sentence in PARTS.finditer(source):
        text = sentence.group()
        if len(text) <= 240:
            yield text
            continue
        spans = list(PROTECTED.finditer(text))
        start = 0
        while start < len(text):
            end = min(start + 180, len(text))
            for span in spans:
                if span.start() < end < span.end():
                    end = span.start() if span.start() > start else span.end()
                    break
            yield text[start:end]
            start = end


def chinese_texts(value):
    if isinstance(value, str):
        return {value} if HAN.search(value) else set()
    if isinstance(value, dict):
        return set().union(*(chinese_texts(v) for v in value.values())) if value else set()
    if isinstance(value, (tuple, list)):
        return set().union(*(chinese_texts(v) for v in value)) if value else set()
    return set()


class EnglishDisplay:
    revision = "argos-zh-en-1.9-domain-v6"

    def __init__(self, root=ROOT, *, infer=None):
        self.root = Path(root)
        self.path = self.root / ".local/console-english/cache.json"
        self.lock = threading.Lock()
        self.infer = infer
        self.sentences = {}
        self.cache = {}
        try:
            saved = json.loads(self.path.read_text())
            if saved.get("revision") == self.revision:
                self.cache = {k: v for k, v in saved["texts"].items() if isinstance(v, str)}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _load(self):
        if self.infer is not None:
            return
        import ctranslate2
        import sentencepiece

        folder = self.root / ".local/translation-models/translate-zh_en-1_9"
        processor = sentencepiece.SentencePieceProcessor(
            model_file=str(folder / "sentencepiece.model")
        )
        engine = ctranslate2.Translator(
            str(folder / "model"),
            device="cpu",
            compute_type="int8",
            intra_threads=2,
            inter_threads=2,
        )

        def infer(texts):
            inputs = [processor.encode(text, out_type=str) for text in texts]
            results = engine.translate_batch(
                inputs,
                beam_size=1,
                max_batch_size=32,
                max_input_length=0,
                max_decoding_length=384,
            )
            return ["".join(r.hypotheses[0]).replace("▁", " ").strip() for r in results]

        self.infer = infer

    def _infer(self, texts):
        missing = list(dict.fromkeys(text for text in texts if text not in self.sentences))
        if missing:
            self.sentences.update(zip(missing, self.infer(missing), strict=True))
        return [self.sentences[text] for text in texts]

    def _translate(self, source):
        if source in GLOSSARY:
            return GLOSSARY[source]
        output = []
        # Bound long paragraphs without cutting through an amount or identifier.
        for part in sentence_parts(source):
            if not HAN.search(part):
                output.append(part)
                continue
            direct = self._infer([part])[0]
            if "积分" in part:
                direct = re.sub(r"\bpoints?\b", "credits", direct, flags=re.IGNORECASE)
            direct = re.sub(r"\bcredites\b", "credits", direct, flags=re.IGNORECASE)
            direct = re.sub(r"\bceramic bug\b", "ceramic mug", direct, flags=re.IGNORECASE)
            required = PROTECTED.findall(part)
            # Prefer fluent sentence translation when all exact values survived.
            # Otherwise use protected markers, then the conservative fragment path.
            if (
                not HAN.search(direct)
                and all(term.lower() in direct.lower() for term in required)
                and re.findall(r"\d+(?:\.\d+)?", direct) == re.findall(r"\d+(?:\.\d+)?", part)
            ):
                output.append(direct)
                continue
            # Keep identifiers and exact numbers outside the translation model's
            # vocabulary. A missing/duplicated marker uses a conservative fallback.
            terms = []

            def protect(value, terms=terms):
                terms.append(value.group())
                return marker(len(terms) - 1)

            masked = PROTECTED.sub(protect, part)
            for original, english in GLOSSARY.items():
                if original in masked:
                    terms.append(english)
                    masked = masked.replace(original, marker(len(terms) - 1), 1)
            result = self._infer([masked])[0]
            valid = not HAN.search(result)
            for index, original in enumerate(terms):
                label = marker(index)
                pattern = re.compile(r"\s*".join(map(re.escape, label)), re.IGNORECASE)
                if len(pattern.findall(result)) != 1:
                    valid = False
                    break
                result = pattern.sub(lambda _, original=original: original, result)
            if not valid:
                # Preserve all ASCII evidence exactly if the sentence model
                # cannot preserve its protected identifiers. Translate Han runs.
                result = re.sub(r"[\u3400-\u9fff]+", lambda m: self._fragment(m.group()), part)
            output.append(result.strip())
        return " ".join(output).replace(" \n ", "\n").strip()

    def _fragment(self, source):
        if source in GLOSSARY:
            return GLOSSARY[source]
        for original, english in GLOSSARY.items():
            if original in source:
                before, _, after = source.partition(original)
                return (
                    (self._fragment(before) if before else "")
                    + " "
                    + english
                    + " "
                    + (self._fragment(after) if after else "")
                )
        translated = self._infer([source])[0]
        return " " + (translated if not HAN.search(translated) else "[untranslated text]") + " "

    def translations(self, texts):
        with self.lock:
            self._load()
            result = {}
            dirty = False
            natural_parts = []
            for source in set(texts):
                if hashlib.sha256(source.encode()).hexdigest() in self.cache:
                    continue
                for part in sentence_parts(source):
                    if not HAN.search(part) or len(part) > 240:
                        continue
                    natural_parts.append(part)
            self._infer(natural_parts)
            for text in sorted(set(texts)):
                if not HAN.search(text):
                    continue
                key = hashlib.sha256(text.encode()).hexdigest()
                if key not in self.cache:
                    self.cache[key] = self._translate(text)
                    dirty = True
                result[text] = self.cache[key]
            if dirty:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temporary = tempfile.mkstemp(dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        json.dump({"revision": self.revision, "texts": self.cache}, stream)
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return result


async def english_view(request, value):
    if request.query_params.get("display") != "en":
        return value
    translator = getattr(request.app.state, "english_display", None)
    if translator is None:
        translator = EnglishDisplay(getattr(request.app.state, "console_root", ROOT))
        request.app.state.english_display = translator
    try:
        texts = await run_in_threadpool(translator.translations, chinese_texts(value))
    except (ImportError, OSError, RuntimeError):
        return {**value, "english": {"available": False, "texts": {}, "local": True}}
    return {**value, "english": {"available": True, "texts": texts, "local": True}}
