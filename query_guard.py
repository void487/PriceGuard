"""Utilities for robust web query rewriting."""

from __future__ import annotations

import re
from dataclasses import dataclass


_WRAPPER_PREFIXES = (
    "user message:",
    "dotaz:",
    "query:",
    "question:",
)


@dataclass(frozen=True)
class QueryRewriteResult:
    """Final query and diagnostics for logging."""

    query: str
    used_fallback: bool
    reason: str


def _normalize(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    return cleaned


def _strip_wrapper_prefix(text: str) -> str:
    t = _normalize(text)
    lowered = t.lower()
    for prefix in _WRAPPER_PREFIXES:
        if lowered.startswith(prefix):
            return _normalize(t[len(prefix) :])
    return t


def _extract_quoted_payload(text: str) -> str:
    quoted = re.findall(r"[\"“”](.+?)[\"“”]", text)
    if quoted:
        return _normalize(max(quoted, key=len))
    return text


def _token_set(text: str) -> set[str]:
    # keep model names like rx9060xt, 16gb, rtx-5090, kč
    tokens = re.findall(r"[\wčěšřžýáíéůúďťň+-]{2,}", text.lower())
    return set(tokens)


def _overlap_ratio(left: str, right: str) -> float:
    left_tokens = _token_set(left)
    right_tokens = _token_set(right)
    if not left_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens)


def finalize_web_query(
    user_text: str,
    rewritten_query: str,
    *,
    min_entity_overlap: float = 0.6,
) -> QueryRewriteResult:
    """Return a production-safe web query.

    Removes wrapper phrasing added by helper LLMs and falls back to the original
    user text when rewrite quality drops too low.
    """

    original = _normalize(user_text)
    rewritten = _normalize(rewritten_query)

    if not rewritten:
        return QueryRewriteResult(query=original, used_fallback=True, reason="empty_rewrite")

    unwrapped = _extract_quoted_payload(_strip_wrapper_prefix(rewritten))
    if not unwrapped:
        return QueryRewriteResult(query=original, used_fallback=True, reason="empty_after_unwrap")

    if _overlap_ratio(original, unwrapped) < min_entity_overlap:
        return QueryRewriteResult(
            query=original,
            used_fallback=True,
            reason="low_entity_overlap",
        )

    return QueryRewriteResult(query=unwrapped, used_fallback=False, reason="ok")
