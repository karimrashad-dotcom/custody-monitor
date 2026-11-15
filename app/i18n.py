"""ترجمة الواجهة للإنجليزي: بنترجم الـ HTML بعد ما يتعمل، من قاموس عربي ← إنجليزي.

الأرقام جوه الجملة بتتحول لـ {0} {1} قبل البحث في القاموس، فجملة زي "4 فاتورة مستنية"
بتتلاقي كـ "{0} فاتورة مستنية". أي كلام مش في القاموس (زي أسماء الموردين) بيفضل زي ما هو.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ARABIC = re.compile(r"[؀-ۿ]")
NUM = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|-?\d[\d,]*(?:\.\d+)?")
TEXT_NODE = re.compile(r">([^<>]+)<")
ATTR = re.compile(r'\b(placeholder|title|aria-label|content)="([^"]*)"')
SKIP_BLOCK = re.compile(r"(<script\b.*?</script>|<style\b.*?</style>|<textarea\b.*?</textarea>)", re.S | re.I)
DICT_PATH = Path(__file__).parent / "i18n_en.json"
missing: set[str] = set()


@lru_cache(maxsize=1)
def table() -> dict[str, str]:
    try:
        return json.loads(DICT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


SPLITTERS = (" · ", ": ", " / ")
LEAD = re.compile(r"^([^\w\u0600-\u06FF(+—#-]+)")


def _lookup(core: str) -> str | None:
    t = table()
    if core in t:
        return t[core]
    nums = NUM.findall(core)
    key = core
    for i, n in enumerate(nums):
        key = key.replace(n, "{%d}" % i, 1)
    if key in t:
        out = t[key]
        for i, n in enumerate(nums):
            out = out.replace("{%d}" % i, n)
        return out
    return None


def _translate_core(core: str, collect: bool) -> str | None:
    out = _lookup(core)
    if out is not None:
        return out
    m = LEAD.match(core)  # إيموجي أو رمز في الأول
    if m and len(m.group(1)) < len(core):
        rest = _translate_core(core[len(m.group(1)):].strip(), collect)
        if rest is not None:
            return m.group(1) + rest
    for sep in SPLITTERS:  # جملة متركبة: كل جزء لوحده، واللي مش في القاموس (أسماء) يفضل زي ما هو
        if sep in core:
            parts = core.split(sep)
            done = [_translate_core(p.strip(), collect) for p in parts]
            if any(d is not None for d in done):
                return sep.join(d if d is not None else p for d, p in zip(done, parts))
    if collect:
        key = core
        for i, n in enumerate(NUM.findall(core)):
            key = key.replace(n, "{%d}" % i, 1)
        missing.add(key)
    return None


def tr(text: str, collect: bool = False) -> str:
    if not ARABIC.search(text):
        return text
    core = " ".join(text.split())
    out = _translate_core(core, collect)
    if out is None:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + out + trail


def translate_html(html: str, collect: bool = False) -> str:
    parts = SKIP_BLOCK.split(html)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # script/style/textarea: زي ما هي
            out.append(part)
            continue
        part = TEXT_NODE.sub(lambda m: ">" + tr(m.group(1), collect) + "<", part)
        part = ATTR.sub(lambda m: f'{m.group(1)}="{tr(m.group(2), collect)}"', part)
        out.append(part)
    return "".join(out)
