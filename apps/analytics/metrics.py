"""Session and conversion totals for the same reporting window Search Console uses."""

from __future__ import annotations

from dataclasses import dataclass

from apps.search_console.metrics import compute_range_window, parse_range_param

__all__ = ["PeriodSummary", "compute_range_window", "parse_range_param", "summarize_period"]


@dataclass
class PeriodSummary:
    sessions: int
    active_users: int
    conversions: int | None
    day_count: int


def summarize_period(rows: list[dict], *, conversions_measurable: bool) -> PeriodSummary:
    sessions = sum(row["sessions"] for row in rows)
    active_users = sum(row["active_users"] for row in rows)
    if not conversions_measurable:
        conversions = None
    else:
        conversions = sum(row["conversions"] or 0 for row in rows)
    return PeriodSummary(sessions=sessions, active_users=active_users, conversions=conversions, day_count=len(rows))


def percent_change(old: int, new: int) -> float | None:
    if old == 0:
        return None
    return ((new - old) / old) * 100
