"""Utilities for guarding against empty assistant responses with citations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class RankedSource:
    """A source item already ranked by retrieval quality (best first)."""

    title: str
    snippet: str
    url: str = ""


def _is_blank(text: str | None) -> bool:
    return text is None or not text.strip()


def _fallback_from_sources(sources: Sequence[RankedSource], max_chars: int) -> str:
    snippets = [s.snippet.strip() for s in sources if s.snippet and s.snippet.strip()]
    if not snippets:
        return ""
    merged = " ".join(snippets)
    return merged[:max_chars].rstrip()


def ensure_body_text(
    body_text: str | None,
    citations: Iterable[str],
    ranked_sources: Sequence[RankedSource],
    synthesizer: Callable[[Sequence[RankedSource]], str],
    top_n: int = 3,
    fallback_max_chars: int = 280,
) -> str:
    """Return the final body text, regenerating when body is empty but citations exist.

    Guard logic:
    - If body text is present, return it unchanged.
    - If body text is missing and there are no citations, return an empty string.
    - If body text is missing and citations exist, synthesize a short answer from top-ranked
      sources.
    - If synthesis fails or still returns blank text, fallback to merged source snippets.
    """

    if not _is_blank(body_text):
        return body_text.strip()

    citation_list = [c for c in citations if str(c).strip()]
    if not citation_list:
        return ""

    top_sources = list(ranked_sources[: max(1, top_n)])
    if not top_sources:
        return ""

    try:
        regenerated = synthesizer(top_sources)
    except Exception:
        regenerated = ""

    if not _is_blank(regenerated):
        return regenerated.strip()

    return _fallback_from_sources(top_sources, max_chars=fallback_max_chars)
