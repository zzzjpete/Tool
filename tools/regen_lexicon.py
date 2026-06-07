"""Regenerate ``scraper/nlp_lexicon.py`` from a build-sentiment-lexicon workflow dump.

The Chinese sentiment lexicon is produced by an LLM fan-out (one agent per
category) and then cleaned deterministically here so the result is reviewable and
reproducible. This script is the documented provenance for ``nlp_lexicon.py`` —
it is NOT imported at runtime.

Usage:
    python tools/regen_lexicon.py <workflow-output.json> [scraper/nlp_lexicon.py]

The input JSON is the workflow's result object, either as the raw
``{"result": {...}}`` task-output envelope or just the inner
``{"pos-general": [...], ...}`` mapping. Each category is a list of
``{"term": str, "value": number}``.

Cleaning rules:
  * terms must be pure CJK (drops yyds / 打call / diss / emoji / digits)
  * POSITIVE/NEGATIVE keep terms with >= 2 chars; DEGREE/NEGATION/STOPWORDS
    allow single chars (太/超/不/没/的 ...)
  * dedupe within a bucket, keeping the max magnitude
  * polarity conflicts (a term in both POSITIVE and NEGATIVE, e.g. 破防) are
    dropped from both — ambiguous words add noise
  * DEGREE terms that are also sentiment words (好/...) are dropped from DEGREE,
    so the sentiment meaning wins in scoring
  * pos/neg weights clamp to [1.0, 2.0]; degree multipliers clamp to [0.3, 2.5]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PURE_CJK = re.compile(r"^[一-鿿㐀-䶿]+$")


def _is_cjk(term: str) -> bool:
    return bool(_PURE_CJK.match(term))


def _clean_weighted(items, *, vmin: float, vmax: float, min_len: int) -> dict[str, float]:
    out: dict[str, float] = {}
    for it in items or []:
        term = str(it.get("term", "")).strip()
        if not _is_cjk(term) or len(term) < min_len:
            continue
        try:
            val = float(it.get("value", 1.0))
        except (TypeError, ValueError):
            continue
        val = round(max(vmin, min(vmax, val)), 2)
        if term not in out or val > out[term]:
            out[term] = val
    return out


def _clean_set(items, *, min_len: int) -> set[str]:
    out: set[str] = set()
    for it in items or []:
        term = str(it.get("term", "")).strip()
        if _is_cjk(term) and len(term) >= min_len:
            out.add(term)
    return out


def build(result: dict) -> dict:
    pos = _clean_weighted(
        (result.get("pos-general") or []) + (result.get("pos-domain") or []),
        vmin=1.0, vmax=2.0, min_len=2,
    )
    neg = _clean_weighted(
        (result.get("neg-general") or []) + (result.get("neg-domain") or []),
        vmin=1.0, vmax=2.0, min_len=2,
    )
    # Polarity conflicts: drop from both buckets.
    conflicts = set(pos) & set(neg)
    for w in conflicts:
        pos.pop(w, None)
        neg.pop(w, None)

    degree = _clean_weighted(result.get("degree"), vmin=0.3, vmax=2.5, min_len=1)
    # A degree word that's also a sentiment word would be matched as sentiment
    # first; keep it out of DEGREE to avoid double duty.
    for w in set(degree) & (set(pos) | set(neg)):
        degree.pop(w, None)

    negation = _clean_set(result.get("negation"), min_len=1)
    stopwords = _clean_set(result.get("stopwords"), min_len=1)
    # Ensure a small essential stopword core is always present.
    stopwords |= {
        "的", "了", "是", "我", "你", "他", "她", "它", "这", "那", "和", "与",
        "就", "都", "也", "在", "对", "把", "被", "给", "吗", "呢", "吧", "啊",
        "什么", "怎么", "可以", "没有", "不是", "一个", "我们", "他们",
    }
    # Negation tokens that double as nothing else; remove any that landed in degree.
    degree = {w: v for w, v in degree.items() if w not in negation}

    return {
        "POSITIVE": pos,
        "NEGATIVE": neg,
        "DEGREE": degree,
        "NEGATION": negation,
        "STOPWORDS": stopwords,
    }


def _fmt_dict(name: str, d: dict[str, float], per_line: int = 4) -> str:
    items = sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = []
    row: list[str] = []
    for term, val in items:
        v = int(val) if float(val).is_integer() else val
        row.append(f'"{term}": {v}')
        if len(row) == per_line:
            lines.append("    " + ", ".join(row) + ",")
            row = []
    if row:
        lines.append("    " + ", ".join(row) + ",")
    body = "\n".join(lines)
    return f"{name}: dict[str, float] = {{\n{body}\n}}"


def _fmt_set(name: str, s: set[str], per_line: int = 10) -> str:
    items = sorted(s)
    lines = []
    row: list[str] = []
    for term in items:
        row.append(f'"{term}"')
        if len(row) == per_line:
            lines.append("    " + ", ".join(row) + ",")
            row = []
    if row:
        lines.append("    " + ", ".join(row) + ",")
    body = "\n".join(lines)
    return f"{name}: set[str] = {{\n{body}\n}}"


_HEADER = '''"""Chinese sentiment lexicon for `scraper.nlp` — AUTO-GENERATED.

Regenerate with: python tools/regen_lexicon.py <workflow-output.json>
Do not hand-edit; edits will be lost on the next regeneration. To tweak, change
the source workflow or the cleaning rules in tools/regen_lexicon.py.

Five buckets:
  * POSITIVE  : word -> intensity weight (1.0 weak ~ 2.0 strong), positive polarity
  * NEGATIVE  : word -> intensity weight (positive number; larger = more negative)
  * DEGREE    : degree adverb -> multiplier applied to the following sentiment word
  * NEGATION  : negators that flip the polarity of the following sentiment word
  * STOPWORDS : high-frequency function words filtered out of keyword extraction

Oriented toward Chinese social-media / news-comment / 舆情 text. Scoring logic
lives in scraper/nlp.py.
"""

from __future__ import annotations

'''

_FOOTER = '\n\n__all__ = ["POSITIVE", "NEGATIVE", "DEGREE", "NEGATION", "STOPWORDS"]\n'


def render(tables: dict) -> str:
    parts = [_HEADER]
    parts.append("# --- Positive sentiment words (1.0 mild ~ 2.0 strong) ---")
    parts.append(_fmt_dict("POSITIVE", tables["POSITIVE"]))
    parts.append("\n# --- Negative sentiment words (magnitude; 1.0 mild ~ 2.0 strong) ---")
    parts.append(_fmt_dict("NEGATIVE", tables["NEGATIVE"]))
    parts.append("\n# --- Degree adverbs (multiplier on the following sentiment word) ---")
    parts.append(_fmt_dict("DEGREE", tables["DEGREE"]))
    parts.append("\n# --- Negators (flip polarity of the following sentiment word) ---")
    parts.append(_fmt_set("NEGATION", tables["NEGATION"]))
    parts.append("\n# --- Stopwords (dropped from keyword/hot-word extraction) ---")
    parts.append(_fmt_set("STOPWORDS", tables["STOPWORDS"]))
    return "\n".join(parts) + _FOOTER


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else Path("scraper/nlp_lexicon.py")
    data = json.loads(src.read_text(encoding="utf-8"))
    result = data.get("result", data)
    tables = build(result)
    text = render(tables)
    out.write_text(text, encoding="utf-8")
    print(
        f"wrote {out}: "
        f"POSITIVE={len(tables['POSITIVE'])} NEGATIVE={len(tables['NEGATIVE'])} "
        f"DEGREE={len(tables['DEGREE'])} NEGATION={len(tables['NEGATION'])} "
        f"STOPWORDS={len(tables['STOPWORDS'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
