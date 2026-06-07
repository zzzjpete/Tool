"""Lightweight Chinese NLP layer — sentiment scoring + keyword extraction.

舆情 (public-opinion) oriented. Two capabilities, both built on `jieba`:

  * ``sentiment(text)`` — lexicon-based polarity scoring with degree-adverb and
    negation handling. Returns a score in [-1, 1] and a 正面/中性/负面 label.
  * ``keywords(texts)`` — TF-IDF hot-word extraction (jieba.analyse) with Chinese
    stopword + noise filtering.

Why a lexicon instead of a trained model: the data here is news/social/政治 text,
not the e-commerce reviews most off-the-shelf Chinese models (e.g. SnowNLP) were
trained on. A transparent lexicon is tunable, dependency-light (only adds jieba),
and good enough for aggregate volume-of-sentiment trends, which is the use case.
The bundled lexicon lives in ``scraper/nlp_lexicon.py``.

jieba is a lazy import (like pandas/matplotlib elsewhere) so importing this module
is cheap and ``pip install``-ing the scraper without jieba still works — the error
only surfaces when you actually call a function that needs it.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Optional

from scraper.core.exceptions import ScraperError
from scraper.nlp_lexicon import (
    POSITIVE,
    NEGATIVE,
    DEGREE,
    NEGATION,
    STOPWORDS,
)

# ---- Tunable scoring constants --------------------------------------------

# How many tokens to look back from a sentiment word for degree/negation modifiers.
_WINDOW = 3
# A negated sentiment word is usually weaker than a plain opposite ("不喜欢" is
# milder than "讨厌"), so dampen its magnitude after flipping the sign.
_NEGATION_DAMP = 0.7
# tanh(raw / _SCALE) squashes the unbounded raw sum into (-1, 1). Larger = gentler.
_SCALE = 4.0
# |score| <= _NEUTRAL_BAND is reported as 中性.
_NEUTRAL_BAND = 0.10

LABEL_POS = "正面"
LABEL_NEG = "负面"
LABEL_NEUTRAL = "中性"

# Split text into clauses so a negation/degree window can't bleed across sentence
# boundaries ("不好，很喜欢" must not let 不 reach 喜欢).
_CLAUSE_SPLIT = re.compile(r"[，。！？；：、\.,!?;:\n\r\t…~　\s]+|[~～]+")
# A token is "wordy" enough to keep as a keyword only if it has a CJK char.
_HAS_CJK = re.compile(r"[一-鿿㐀-䶿]")


# ---- jieba lazy import -----------------------------------------------------


_USERDICT_LOADED = False


def _load_userdict(jieba: Any) -> None:
    """Register multi-char sentiment/degree terms as jieba words (idempotent).

    Without this, idioms like 叹为观止 / 虚假宣传 / 割韭菜 get split by jieba's
    default segmenter and never match the lexicon. Adding them as words makes
    jieba emit each as a single token, so the matcher in `sentiment()` sees them.
    Single-char entries are skipped — jieba already tokenizes those atomically.
    """
    global _USERDICT_LOADED
    if _USERDICT_LOADED:
        return
    for table in (POSITIVE, NEGATIVE, DEGREE):
        for word in table:
            if len(word) >= 2:
                try:
                    jieba.add_word(word)
                except Exception:
                    pass
    _USERDICT_LOADED = True


def _jieba() -> Any:
    """Lazy import jieba; suppress its noisy pkg_resources deprecation warning."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba  # noqa: F401
        _load_userdict(jieba)
        return jieba
    except ImportError as e:  # pragma: no cover - exercised only without jieba
        raise ScraperError(
            "jieba is not installed. Run: pip install jieba"
        ) from e


def _jieba_analyse() -> Any:
    # Ensure the shared jieba dictionary has our terms before extraction runs.
    _jieba()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba.analyse as analyse  # noqa: F401
        return analyse
    except ImportError as e:  # pragma: no cover
        raise ScraperError("jieba is not installed. Run: pip install jieba") from e


# ---- Text cleaning ---------------------------------------------------------


def strip_html(text: Optional[str]) -> str:
    """Strip HTML tags → plain text. Zhihu answer/article content is stored as raw
    HTML; bili descriptions are plain. Returns ``""`` for None/empty.

    bs4 is already a hard dependency of the scraper, so no lazy-import dance.
    """
    if not text:
        return ""
    if "<" not in text:
        return text
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(text, "lxml").get_text(separator=" ")
    except Exception:
        # Last-resort regex strip if bs4/lxml ever misbehaves on weird markup.
        return re.sub(r"<[^>]+>", " ", text)


def clean_text(text: Optional[str]) -> str:
    """HTML-strip + whitespace-collapse. The canonical pre-processing step."""
    s = strip_html(text)
    return re.sub(r"\s+", " ", s).strip()


# ---- Sentiment -------------------------------------------------------------


@dataclass(frozen=True)
class SentimentResult:
    """Outcome of scoring one piece of text.

    score  : normalized polarity in [-1, 1] (tanh-squashed).
    label  : 正面 / 中性 / 负面.
    raw    : the un-squashed signed sum (useful for debugging / re-thresholding).
    pos_hits / neg_hits : count of positive / negative lexicon words matched.
    """

    score: float
    label: str
    raw: float
    pos_hits: int
    neg_hits: int


def label_for(score: float) -> str:
    if score > _NEUTRAL_BAND:
        return LABEL_POS
    if score < -_NEUTRAL_BAND:
        return LABEL_NEG
    return LABEL_NEUTRAL


def sentiment(text: Optional[str]) -> SentimentResult:
    """Score one piece of text. Empty / sentiment-free text → 中性 (score 0).

    Algorithm (per clause, summed across clauses):
      walk tokens; on a sentiment word, look back up to ``_WINDOW`` tokens for
      degree adverbs (multiply magnitude) and negators (flip sign, odd count only,
      then dampen). Another sentiment word ends the modifier scope. Sum signed
      contributions, then ``tanh(sum / _SCALE)`` for a bounded score.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return SentimentResult(0.0, LABEL_NEUTRAL, 0.0, 0, 0)

    jieba = _jieba()
    total = 0.0
    pos_hits = 0
    neg_hits = 0

    for clause in _CLAUSE_SPLIT.split(cleaned):
        if not clause:
            continue
        tokens = list(jieba.cut(clause))
        for idx, tok in enumerate(tokens):
            if tok in POSITIVE:
                base, sign = POSITIVE[tok], 1
            elif tok in NEGATIVE:
                base, sign = NEGATIVE[tok], -1
            else:
                continue

            mult = 1.0
            neg_count = 0
            steps = 0
            j = idx - 1
            while j >= 0 and steps < _WINDOW:
                prev = tokens[j]
                if prev in DEGREE:
                    mult *= DEGREE[prev]
                elif prev in NEGATION:
                    neg_count += 1
                elif prev in POSITIVE or prev in NEGATIVE:
                    break  # modifier scope stops at the previous sentiment word
                j -= 1
                steps += 1

            if neg_count % 2 == 1:
                sign = -sign
                mult *= _NEGATION_DAMP

            total += sign * base * mult
            if sign > 0:
                pos_hits += 1
            else:
                neg_hits += 1

    score = _tanh(total / _SCALE)
    return SentimentResult(score, label_for(score), total, pos_hits, neg_hits)


def _tanh(x: float) -> float:
    import math

    return math.tanh(x)


# ---- Keyword / hot-word extraction ----------------------------------------


def tokenize(text: Optional[str], *, drop_stopwords: bool = True) -> list[str]:
    """jieba tokens with noise removed: whitespace, punctuation, pure-ASCII tokens,
    single characters, and (optionally) stopwords. Used for word-frequency views.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    jieba = _jieba()
    out: list[str] = []
    for tok in jieba.cut(cleaned):
        tok = tok.strip()
        if len(tok) < 2:
            continue
        if not _HAS_CJK.search(tok):
            continue  # drop pure ASCII / digit tokens ("but", "2024", ...)
        if drop_stopwords and tok in STOPWORDS:
            continue
        out.append(tok)
    return out


def keywords(
    texts: Iterable[str],
    *,
    topK: int = 30,
    exclude: Iterable[str] = (),
) -> list[tuple[str, float]]:
    """TF-IDF hot words across a corpus of texts. Returns ``[(word, weight), ...]``.

    Filters out stopwords, single chars, pure-ASCII/number tokens, and any term in
    ``exclude`` (pass the search keyword so it doesn't dominate its own report).
    jieba.analyse ships a default IDF table, so this works with zero corpus setup.
    """
    analyse = _jieba_analyse()
    corpus = "\n".join(clean_text(t) for t in texts if t)
    if not corpus.strip():
        return []
    # Expand each exclude term through the same tokenization the corpus gets, so a
    # multi-token or punctuation-bearing search keyword (e.g. "电动车（油电混合）")
    # still excludes the words jieba actually extracts (电动车 / 油电 / 混合) — a raw
    # full-string compare would miss them.
    exclude_set: set[str] = set()
    for e in exclude:
        if not e:
            continue
        exclude_set.add(clean_text(e))
        exclude_set.update(tokenize(e, drop_stopwords=False))
    # Over-fetch then filter, so we still return ~topK after dropping noise.
    raw = analyse.extract_tags(corpus, topK=topK * 3, withWeight=True)
    out: list[tuple[str, float]] = []
    for word, weight in raw:
        word = word.strip()
        if len(word) < 2 or not _HAS_CJK.search(word):
            continue
        if word in STOPWORDS or word in exclude_set:
            continue
        out.append((word, float(weight)))
        if len(out) >= topK:
            break
    return out


@lru_cache(maxsize=1)
def lexicon_stats() -> dict[str, int]:
    """Sizes of each lexicon bucket — surfaced by `doctor` / for sanity checks."""
    return {
        "positive": len(POSITIVE),
        "negative": len(NEGATIVE),
        "degree": len(DEGREE),
        "negation": len(NEGATION),
        "stopwords": len(STOPWORDS),
    }


__all__ = [
    "SentimentResult",
    "sentiment",
    "label_for",
    "tokenize",
    "keywords",
    "strip_html",
    "clean_text",
    "lexicon_stats",
    "LABEL_POS",
    "LABEL_NEG",
    "LABEL_NEUTRAL",
]
