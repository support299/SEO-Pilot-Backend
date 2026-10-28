import logging
import math
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.businesses.models import Business
from integrations.dataforseo import client as dfs

from .models import GridPoint, GridScan, LocalVisibilityProfile, TrackedKeyword

logger = logging.getLogger("django")

KM_PER_DEGREE_LAT = 111.32
MAX_DEPTH = 20  # we look at the first 20 Maps results per point; beyond that counts as "not ranking"
MAX_PARALLEL_CHECKS = 5
ALLOWED_GRID_SIZES = (3, 5, 7)
MAX_RADIUS_KM = 50
QUEUED_TIMEOUT = timedelta(minutes=5)  # a queued scan nobody picked up: the worker was down or didn't know the task
RUNNING_TIMEOUT = timedelta(minutes=10)


class LocalVisibilityError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --- Matching our business in Google Maps results ---------------------------


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "//" not in value:
        value = "//" + value
    host = urlparse(value).netloc or ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def our_domain(business: Business, profile: LocalVisibilityProfile) -> str:
    return normalize_domain(profile.business_domain or business.website or "")


def find_rank(items: list[dict], domain: str, place_id: str) -> int | None:
    for item in items:
        if place_id and item.get("place_id") == place_id:
            return item["rank"]
        item_domain = normalize_domain(item.get("domain") or "")
        if domain and item_domain and (item_domain == domain or item_domain.endswith("." + domain)):
            return item["rank"]
    return None


# --- Grid maths --------------------------------------------------------------


def grid_points(latitude: float, longitude: float, size: int, radius_km: float) -> list[tuple[int, int, float, float]]:
    """(row, col, lat, lng) for a size x size grid centred on the point; row 0 is the northern edge."""
    step_km = (2 * radius_km / (size - 1)) if size > 1 else 0
    lng_km = KM_PER_DEGREE_LAT * max(math.cos(math.radians(latitude)), 0.01)
    half = (size - 1) / 2
    points = []
    for row in range(size):
        for col in range(size):
            north_km = (half - row) * step_km
            east_km = (col - half) * step_km
            points.append((row, col, latitude + north_km / KM_PER_DEGREE_LAT, longitude + east_km / lng_km))
    return points


def zoom_for_spacing(size: int, radius_km: float) -> int:
    spacing = (2 * radius_km / (size - 1)) if size > 1 else radius_km
    if spacing <= 1:
        return 16
    if spacing <= 3:
        return 15
    if spacing <= 8:
        return 14
    return 13


def point_score(rank: int | None) -> float:
    """1.0 for rank 1 down to 0.05 for rank 20; nothing for not ranking."""
    if rank is None or rank > MAX_DEPTH:
        return 0.0
    return (MAX_DEPTH + 1 - rank) / MAX_DEPTH


def summarize_ranks(ranks: list[int | None]) -> dict:
    """`ranks` holds one entry per point that was actually checked (failed checks are left out)."""
    total = len(ranks)
    ranked = [rank for rank in ranks if rank is not None]
    if total == 0:
        return {"average_rank": None, "visibility_score": None, "top3_coverage": None, "top10_coverage": None, "ranked_points": 0}
    return {
        "average_rank": round(sum(ranked) / len(ranked), 1) if ranked else None,
        "visibility_score": round(100 * sum(point_score(rank) for rank in ranks) / total, 1),
        "top3_coverage": round(100 * sum(1 for rank in ranked if rank <= 3) / total, 1),
        "top10_coverage": round(100 * sum(1 for rank in ranked if rank <= 10) / total, 1),
        "ranked_points": len(ranked),
    }


# --- Setup -------------------------------------------------------------------


def save_profile(business: Business, *, latitude: float, longitude: float, grid_size: int, radius_km: float, business_domain: str = "", place_id: str = "") -> LocalVisibilityProfile:
    if grid_size not in ALLOWED_GRID_SIZES:
        raise LocalVisibilityError(f"Grid size must be one of {', '.join(f'{n}x{n}' for n in ALLOWED_GRID_SIZES)}.")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise LocalVisibilityError("Those coordinates are not a valid location.")
    if not (0.5 <= radius_km <= MAX_RADIUS_KM):
        raise LocalVisibilityError(f"The radius must be between 0.5 and {MAX_RADIUS_KM} km.")
    profile, _ = LocalVisibilityProfile.objects.update_or_create(
        business=business,
        defaults={
            "latitude": latitude,
            "longitude": longitude,
            "grid_size": grid_size,
            "radius_km": radius_km,
            "business_domain": normalize_domain(business_domain),
            "place_id": place_id.strip(),
        },
    )
    return profile


def add_keyword(business: Business, keyword: str) -> TrackedKeyword:
    keyword = " ".join(keyword.split())
    if not keyword:
        raise LocalVisibilityError("Enter a keyword to track.")
    if TrackedKeyword.objects.filter(business=business, keyword__iexact=keyword).exists():
        raise LocalVisibilityError("That keyword is already being tracked.", 409)
    if TrackedKeyword.objects.filter(business=business).count() >= settings.LOCAL_VISIBILITY_MAX_KEYWORDS_PER_BUSINESS:
        raise LocalVisibilityError(f"You can track up to {settings.LOCAL_VISIBILITY_MAX_KEYWORDS_PER_BUSINESS} keywords per business.", 429)
    return TrackedKeyword.objects.create(business=business, keyword=keyword)


# --- Scanning ----------------------------------------------------------------


def scans_today(business: Business) -> int:
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return GridScan.objects.filter(business=business, created_at__gte=start).count()


def expire_stale_scans(business: Business) -> None:
    """Fail scans that stopped making progress, so they don't show "Scanning…" forever or block the next scan."""
    now = timezone.now()
    stale = GridScan.objects.filter(business=business).filter(
        models.Q(status=GridScan.Status.QUEUED, created_at__lt=now - QUEUED_TIMEOUT)
        | models.Q(status=GridScan.Status.RUNNING, started_at__lt=now - RUNNING_TIMEOUT)
    )
    stale.update(
        status=GridScan.Status.FAILED,
        error="The scan didn't finish. Check that the Celery worker is running, then scan again.",
        finished_at=now,
    )


def start_scan(keyword: TrackedKeyword) -> GridScan:
    business = keyword.business
    expire_stale_scans(business)
    profile = LocalVisibilityProfile.objects.filter(business=business).first()
    if not profile:
        raise LocalVisibilityError("Set the business location before running a scan.")
    if not dfs.is_configured():
        raise LocalVisibilityError("Rank tracking isn't set up on the server yet (missing DataForSEO credentials).", 503)
    if GridScan.objects.filter(keyword=keyword, status__in=[GridScan.Status.QUEUED, GridScan.Status.RUNNING]).exists():
        raise LocalVisibilityError("A scan is already running for this keyword.", 409)
    if scans_today(business) >= settings.LOCAL_VISIBILITY_MAX_SCANS_PER_BUSINESS_PER_DAY:
        raise LocalVisibilityError(f"Daily scan limit reached ({settings.LOCAL_VISIBILITY_MAX_SCANS_PER_BUSINESS_PER_DAY} per business). Try again tomorrow.", 429)

    return GridScan.objects.create(
        keyword=keyword,
        business=business,
        grid_size=profile.grid_size,
        radius_km=profile.radius_km,
        center_latitude=profile.latitude,
        center_longitude=profile.longitude,
    )


def _check_point(keyword: str, latitude: float, longitude: float, zoom: int):
    try:
        return dfs.search_maps(keyword, latitude, longitude, zoom, depth=MAX_DEPTH)
    except dfs.DataForSeoError as exc:
        return exc


def execute_scan(scan_id: int) -> None:
    scan = GridScan.objects.select_related("keyword", "business").filter(id=scan_id).first()
    if not scan or scan.status != GridScan.Status.QUEUED:
        return
    profile = LocalVisibilityProfile.objects.filter(business=scan.business).first()

    scan.status = GridScan.Status.RUNNING
    scan.started_at = timezone.now()
    scan.save(update_fields=["status", "started_at"])

    domain = our_domain(scan.business, profile) if profile else normalize_domain(scan.business.website or "")
    place_id = profile.place_id if profile else ""
    if not domain and not place_id:
        _fail_scan(scan, "Add the business website (or a Google place ID) so we can recognise it in the results.")
        return

    points = grid_points(scan.center_latitude, scan.center_longitude, scan.grid_size, scan.radius_km)
    zoom = zoom_for_spacing(scan.grid_size, scan.radius_km)

    try:
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHECKS) as pool:
            results = list(pool.map(lambda p: _check_point(scan.keyword.keyword, p[2], p[3], zoom), points))
    except Exception as exc:  # never leave a scan stuck in "running"
        logger.exception("Grid scan %s crashed", scan.id)
        _fail_scan(scan, f"The scan stopped unexpectedly: {exc}")
        return

    failures = [r for r in results if isinstance(r, Exception)]
    if len(failures) * 2 > len(results):
        _fail_scan(scan, str(failures[0]), cost=_total_cost(results))
        return

    rows, checked_ranks = [], []
    for (row, col, lat, lng), result in zip(points, results):
        if isinstance(result, Exception):
            rows.append(GridPoint(scan=scan, row=row, col=col, latitude=lat, longitude=lng, rank=None, failed=True))
            continue
        rank = find_rank(result.items, domain, place_id)
        checked_ranks.append(rank)
        rows.append(GridPoint(scan=scan, row=row, col=col, latitude=lat, longitude=lng, rank=rank, top_results=_top_results(result.items)))
    GridPoint.objects.bulk_create(rows)

    summary = summarize_ranks(checked_ranks)
    scan.status = GridScan.Status.COMPLETED
    scan.average_rank = summary["average_rank"]
    scan.visibility_score = summary["visibility_score"]
    scan.top3_coverage = summary["top3_coverage"]
    scan.top10_coverage = summary["top10_coverage"]
    scan.ranked_points = summary["ranked_points"]
    scan.cost_usd = _total_cost(results)
    scan.finished_at = timezone.now()
    scan.error = f"{len(failures)} of {len(results)} checks failed and were left out." if failures else ""
    scan.save()


def _total_cost(results) -> Decimal:
    return sum((Decimal(str(r.cost)) for r in results if not isinstance(r, Exception)), Decimal("0"))


def _top_results(items: list[dict]) -> list[dict]:
    return [{k: item[k] for k in ("rank", "title", "domain", "rating", "reviews")} for item in items[:3]]


def _fail_scan(scan: GridScan, message: str, cost: Decimal = Decimal("0")) -> None:
    scan.status = GridScan.Status.FAILED
    scan.error = message
    scan.cost_usd = cost
    scan.finished_at = timezone.now()
    scan.save(update_fields=["status", "error", "cost_usd", "finished_at"])


def scan_all_businesses() -> int:
    """Used by the optional weekly schedule. Respects the same daily caps as a manual scan."""
    started = 0
    for keyword in TrackedKeyword.objects.select_related("business").filter(business__local_visibility_profile__isnull=False):
        try:
            scan = start_scan(keyword)
        except LocalVisibilityError:
            continue
        from .tasks import run_grid_scan

        run_grid_scan.delay(scan.id)
        started += 1
    return started


# --- Reading -----------------------------------------------------------------


def scan_summary(scan: GridScan | None, *, include_cost: bool = False) -> dict | None:
    if scan is None:
        return None
    data = {
        "scan_id": scan.id,
        "status": scan.status,
        "finished_at": scan.finished_at,
        "average_rank": scan.average_rank,
        "visibility_score": scan.visibility_score,
        "top3_coverage": scan.top3_coverage,
        "top10_coverage": scan.top10_coverage,
        "ranked_points": scan.ranked_points,
        "points_total": scan.grid_size * scan.grid_size,
        "error": scan.error,
    }
    if include_cost:
        data["cost_usd"] = float(scan.cost_usd)
    return data


def serialize_profile(profile: LocalVisibilityProfile | None, business: Business) -> dict | None:
    if profile is None:
        return None
    return {
        "latitude": profile.latitude,
        "longitude": profile.longitude,
        "grid_size": profile.grid_size,
        "radius_km": profile.radius_km,
        "business_domain": profile.business_domain,
        "place_id": profile.place_id,
        "matching_domain": our_domain(business, profile),
    }


def _completed(keyword: TrackedKeyword):
    return keyword.scans.filter(status=GridScan.Status.COMPLETED)


def overview(business: Business, *, include_cost: bool = False) -> dict:
    expire_stale_scans(business)
    profile = LocalVisibilityProfile.objects.filter(business=business).first()
    keywords = []
    latest_scores = []
    improving = declining = stable = 0
    for keyword in TrackedKeyword.objects.filter(business=business):
        completed = list(_completed(keyword)[:2])
        latest = completed[0] if completed else None
        previous = completed[1] if len(completed) > 1 else None
        active = keyword.scans.filter(status__in=[GridScan.Status.QUEUED, GridScan.Status.RUNNING]).exists()
        last_scan = keyword.scans.first()
        keywords.append(
            {
                "id": keyword.id,
                "keyword": keyword.keyword,
                "scanning": active,
                "last_error": last_scan.error if last_scan and last_scan.status == GridScan.Status.FAILED else "",
                "latest": scan_summary(latest, include_cost=include_cost),
                "previous": scan_summary(previous, include_cost=include_cost),
            }
        )
        if latest:
            latest_scores.append(latest)
            if previous and latest.visibility_score is not None and previous.visibility_score is not None:
                delta = latest.visibility_score - previous.visibility_score
                if delta > 1:
                    improving += 1
                elif delta < -1:
                    declining += 1
                else:
                    stable += 1

    def average(attr):
        values = [getattr(scan, attr) for scan in latest_scores if getattr(scan, attr) is not None]
        return round(sum(values) / len(values), 1) if values else None

    return {
        "configured": dfs.is_configured(),
        "profile": serialize_profile(profile, business),
        "limits": {
            "keywords_max": settings.LOCAL_VISIBILITY_MAX_KEYWORDS_PER_BUSINESS,
            "scans_per_day": settings.LOCAL_VISIBILITY_MAX_SCANS_PER_BUSINESS_PER_DAY,
            "scans_today": scans_today(business),
            "grid_sizes": list(ALLOWED_GRID_SIZES),
        },
        "metrics": {
            "average_rank": average("average_rank"),
            "visibility_score": average("visibility_score"),
            "top3_coverage": average("top3_coverage"),
            "top10_coverage": average("top10_coverage"),
            "improving": improving,
            "declining": declining,
            "stable": stable,
        },
        "keywords": keywords,
    }


def keyword_detail(keyword: TrackedKeyword, *, include_cost: bool = False) -> dict:
    completed = list(_completed(keyword)[:2])
    latest = completed[0] if completed else None
    previous = completed[1] if len(completed) > 1 else None
    previous_ranks = {(p.row, p.col): p.rank for p in previous.points.all()} if previous else {}

    points = []
    if latest:
        for point in latest.points.all():
            points.append(
                {
                    "row": point.row,
                    "col": point.col,
                    "latitude": point.latitude,
                    "longitude": point.longitude,
                    "rank": point.rank,
                    "previous_rank": previous_ranks.get((point.row, point.col)),
                    "failed": point.failed,
                    "top_results": point.top_results,
                }
            )

    active = keyword.scans.filter(status__in=[GridScan.Status.QUEUED, GridScan.Status.RUNNING]).first()
    history = [scan_summary(scan, include_cost=include_cost) for scan in reversed(list(_completed(keyword)[:30]))]
    return {
        "keyword": {"id": keyword.id, "keyword": keyword.keyword},
        "scanning": active is not None,
        "scan": (
            {
                **scan_summary(latest, include_cost=include_cost),
                "grid_size": latest.grid_size,
                "radius_km": latest.radius_km,
                "center_latitude": latest.center_latitude,
                "center_longitude": latest.center_longitude,
                "points": points,
            }
            if latest
            else None
        ),
        "previous": scan_summary(previous, include_cost=include_cost),
        "history": history,
    }
