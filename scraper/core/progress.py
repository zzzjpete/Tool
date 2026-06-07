"""Structured progress events for scrape_keyword.

The CLI binds these to a tqdm bar; passing free-form strings still works for
back-compat (callbacks see them as plain `str` items). Keep this module
dependency-free — the scraper layer must not import tqdm itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class TotalEvent:
    """Emitted once after the search step, when the scraper knows how many items
    it's going to walk. The CLI uses this to size the progress bar."""
    total: int
    label: str = ""


@dataclass
class StepEvent:
    """Emitted after each item finishes — success or failure. The CLI uses ok
    to update the bar's ✓/✗ counters and groups failures by `error` (the
    exception class name, e.g. 'SoftBanned')."""
    ok: bool
    error: str = ""
    extra: dict = field(default_factory=dict)


ProgressItem = Union[str, TotalEvent, StepEvent]
