"""Utilities for final response body handling."""

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


def ensure_body_text(
    body_text: str | None,
    citations: Iterable[str],
    ranked_sources: Sequence[RankedSource],
    synthesizer: Callable[[Sequence[RankedSource]], str],
    top_n: int = 3,
    fallback_max_chars: int = 280,
) -> str:
    """Return final body text without workaround regeneration.

    The previous temporary behavior regenerated text from citations/snippets when body text was
    empty. That workaround masked upstream orchestration defects and could yield misleading
    answers. The response layer now returns body text only when the upstream model actually
    produced it.
    """

    if not _is_blank(body_text):
        return body_text.strip()

    return ""
