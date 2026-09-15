"""
Pure functions — no Django/DB access — ported from the old app's
src/lib/search-console/{date-ranges,metrics}.ts, where this exact math was
already unit-verified. No behavior changes; only the language changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

RANGE_OPTIONS = (7, 28, 90)
DEFAULT_RANGE = 28
REPORTING_LAG_DAYS = 2  # Search Console data isn't "final" for the most recent ~2 days.


def parse_range_param(value: str | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_RANGE
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return parsed if parsed in RANGE_OPTIONS else DEFAULT_RANGE


@dataclass
class DateWindow:
    start: date
    end: date


@dataclass
class RangeWindow:
    current: DateWindow
    previous: DateWindow


def compute_range_window(days: int, today: date | None = None) -> RangeWindow:
    today = today or date.today()
    end = today - timedelta(days=REPORTING_LAG_DAYS)
    start = end - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return RangeWindow(
        current=DateWindow(start=start, end=end),
        previous=DateWindow(start=previous_start, end=previous_end),
    )


@dataclass
class PeriodSummary:
    clicks: int
    impressions: int
    ctr: float | None
    average_position: float | None
    day_count: int


def summarize_period(rows: list[dict]) -> PeriodSummary:
    """Each row: {clicks, impressions, position}. CTR/position are recomputed from totals — not averaged per-day — so they're accurate regardless of how traffic is distributed across days."""
    clicks = sum(r["clicks"] for r in rows)
    impressions = sum(r["impressions"] for r in rows)
    weighted_position = sum(r["position"] * r["impressions"] for r in rows)

    return PeriodSummary(
        clicks=clicks,
        impressions=impressions,
        ctr=(clicks / impressions) if impressions > 0 else None,
        average_position=(weighted_position / impressions) if impressions > 0 else None,
        day_count=len(rows),
    )


@dataclass
class MetricComparison:
    clicks_delta_pct: float | None
    impressions_delta_pct: float | None
    ctr_delta_pct: float | None
    position_delta: float | None  # negative = improved (moved up the results)


def _percent_change(old: float, new: float) -> float | None:
    if old == 0:
        return None
    return ((new - old) / old) * 100


def compare_periods(current: PeriodSummary, previous: PeriodSummary) -> MetricComparison:
    return MetricComparison(
        clicks_delta_pct=_percent_change(previous.clicks, current.clicks),
        impressions_delta_pct=_percent_change(previous.impressions, current.impressions),
        ctr_delta_pct=(_percent_change(previous.ctr, current.ctr) if current.ctr is not None and previous.ctr is not None else None),
        position_delta=(current.average_position - previous.average_position) if current.average_position is not None and previous.average_position is not None else None,
    )


def has_comparable_data(summary: PeriodSummary) -> bool:
    return summary.impressions > 0
