import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

from apps.businesses.models import Account, Business, Membership
from integrations.dataforseo.client import DataForSeoError, MapsResult, parse_maps_response

from . import services
from .models import GridScan, TrackedKeyword

pytestmark = pytest.mark.django_db


def test_grid_is_centred_and_sized_correctly():
    points = services.grid_points(29.76, -95.36, 5, 5)
    assert len(points) == 25
    centre = points[12]
    assert (round(centre[2], 6), round(centre[3], 6)) == (29.76, -95.36)
    assert points[0][2] > points[24][2]  # row 0 is the northern edge
    assert points[0][3] < points[24][3]  # col 0 is the western edge


def test_normalize_domain_and_matching():
    assert services.normalize_domain("https://www.TheServicePilot.com/get-your-demo") == "theservicepilot.com"
    assert services.normalize_domain("theservicepilot.com") == "theservicepilot.com"
    items = [
        {"rank": 1, "domain": "other.com", "place_id": "a"},
        {"rank": 2, "domain": "www.theservicepilot.com", "place_id": "b"},
    ]
    assert services.find_rank(items, "theservicepilot.com", "") == 2
    assert services.find_rank(items, "nope.com", "a") == 1
    assert services.find_rank(items, "nope.com", "") is None


def test_summary_counts_failed_checks_out_and_scores_unranked_as_zero():
    summary = services.summarize_ranks([1, 3, 8, None])
    assert summary["average_rank"] == 4.0
    assert summary["top3_coverage"] == 50.0
    assert summary["top10_coverage"] == 75.0
    assert summary["visibility_score"] == round(100 * (1 + 18 / 20 + 13 / 20 + 0) / 4, 1)
    assert services.summarize_ranks([])["visibility_score"] is None


def test_parse_maps_response_and_errors():
    ok = parse_maps_response(
        {
            "status_code": 20000,
            "cost": 0.002,
            "tasks": [{"status_code": 20000, "result": [{"items": [{"type": "maps_search", "rank_absolute": 4, "title": "A", "domain": "a.com", "place_id": "x", "rating": {"value": 4.5, "votes_count": 9}}, {"type": "paid"}]}]}],
        }
    )
    assert ok.cost == 0.002 and len(ok.items) == 1 and ok.items[0]["rank"] == 4 and ok.items[0]["reviews"] == 9
    with pytest.raises(DataForSeoError):
        parse_maps_response({"status_code": 20000, "tasks": [{"status_code": 40200, "status_message": "Payment Required."}]})


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def business():
    account = Account.objects.create(name="Acme")
    return Business.objects.create(account=account, name="Acme", website="https://www.acme.example/")


def _login(client, email, *, account=None, role=None, admin=False):
    user = get_user_model().objects.create_user(email=email, password="SuperSecret123!", is_agency_admin=admin)
    if account:
        Membership.objects.create(account=account, user=user, role=role)
    client.force_authenticate(user)
    return user


def _fake_search(monkeypatch, *, rank_for=lambda lat, lng: 2):
    def fake(keyword, latitude, longitude, zoom, depth=20, language_code="en"):
        rank = rank_for(latitude, longitude)
        items = [{"rank": 1, "title": "Rival", "domain": "rival.com", "place_id": "r", "rating": 4.0, "reviews": 3, "address": ""}]
        if rank:
            items.insert(rank - 1, {"rank": rank, "title": "Acme", "domain": "acme.example", "place_id": "a", "rating": 5, "reviews": 1, "address": ""})
        return MapsResult(items=items, cost=0.002)

    monkeypatch.setattr("apps.local_visibility.services.dfs.search_maps", fake)
    monkeypatch.setattr("apps.local_visibility.services.dfs.is_configured", lambda: True)
    monkeypatch.setattr("apps.local_visibility.views.run_grid_scan.delay", lambda scan_id: services.execute_scan(scan_id))


def _setup(client, business):
    client.put(reverse("local_visibility:profile", args=[business.id]), {"latitude": 29.76, "longitude": -95.36, "grid_size": 3, "radius_km": 2}, format="json")
    response = client.post(reverse("local_visibility:keywords", args=[business.id]), {"keyword": "seo services"}, format="json")
    return response.json()["id"]


def test_full_scan_flow_and_viewer_is_read_only(client, business, monkeypatch):
    _fake_search(monkeypatch)
    _login(client, "owner@acme.com", account=business.account, role=Membership.Role.OWNER)
    keyword_id = _setup(client, business)

    response = client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id]))
    assert response.status_code == 202

    detail = client.get(reverse("local_visibility:keyword-detail", args=[business.id, keyword_id])).json()
    assert detail["scan"]["status"] == "completed"
    assert len(detail["scan"]["points"]) == 9
    assert all(point["rank"] == 2 for point in detail["scan"]["points"])
    assert detail["scan"]["average_rank"] == 2.0
    assert "cost_usd" not in detail["scan"]  # costs are for the agency only

    overview = client.get(reverse("local_visibility:overview", args=[business.id])).json()
    assert overview["metrics"]["top3_coverage"] == 100.0 and overview["limits"]["scans_today"] == 1

    viewer = APIClient()
    _login(viewer, "customer@acme.com", account=business.account, role=Membership.Role.VIEWER)
    assert viewer.get(reverse("local_visibility:overview", args=[business.id])).status_code == 200
    assert viewer.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id])).status_code == 403
    assert viewer.post(reverse("local_visibility:keywords", args=[business.id]), {"keyword": "x"}, format="json").status_code == 403


def test_stranger_gets_404_and_admin_sees_cost(client, business, monkeypatch):
    _fake_search(monkeypatch)
    _login(client, "owner2@acme.com", account=business.account, role=Membership.Role.OWNER)
    keyword_id = _setup(client, business)
    client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id]))

    stranger = APIClient()
    _login(stranger, "nobody@else.com")
    assert stranger.get(reverse("local_visibility:overview", args=[business.id])).status_code == 404

    boss = APIClient()
    _login(boss, "boss@agency.com", admin=True)
    detail = boss.get(reverse("local_visibility:keyword-detail", args=[business.id, keyword_id])).json()
    assert detail["scan"]["cost_usd"] == pytest.approx(0.018)


def test_scan_needs_profile_and_respects_daily_cap_and_duplicates(client, business, monkeypatch, settings):
    _fake_search(monkeypatch)
    _login(client, "owner3@acme.com", account=business.account, role=Membership.Role.OWNER)
    keyword = TrackedKeyword.objects.create(business=business, keyword="plumber")
    assert client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword.id])).status_code == 400  # no location yet

    keyword_id = _setup(client, business)
    settings.LOCAL_VISIBILITY_MAX_SCANS_PER_BUSINESS_PER_DAY = 1
    assert client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id])).status_code == 202
    assert client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id])).status_code == 429

    duplicate = client.post(reverse("local_visibility:keywords", args=[business.id]), {"keyword": "SEO Services"}, format="json")
    assert duplicate.status_code == 409


def test_scan_fails_cleanly_when_provider_is_down(client, business, monkeypatch):
    _fake_search(monkeypatch)

    def broken(*args, **kwargs):
        raise DataForSeoError("Payment Required.")

    monkeypatch.setattr("apps.local_visibility.services.dfs.search_maps", broken)
    _login(client, "owner4@acme.com", account=business.account, role=Membership.Role.OWNER)
    keyword_id = _setup(client, business)
    client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id]))
    scan = GridScan.objects.get()
    assert scan.status == GridScan.Status.FAILED and "Payment Required" in scan.error
    overview = client.get(reverse("local_visibility:overview", args=[business.id])).json()
    assert overview["keywords"][0]["last_error"].startswith("Payment Required")


def test_stuck_scan_is_failed_so_the_keyword_can_be_scanned_again(client, business, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    _fake_search(monkeypatch)
    _login(client, "owner5@acme.com", account=business.account, role=Membership.Role.OWNER)
    keyword_id = _setup(client, business)
    stuck = GridScan.objects.create(keyword_id=keyword_id, business=business, grid_size=3, radius_km=2, center_latitude=29.76, center_longitude=-95.36)
    GridScan.objects.filter(id=stuck.id).update(created_at=timezone.now() - timedelta(minutes=30))

    overview = client.get(reverse("local_visibility:overview", args=[business.id])).json()
    assert overview["keywords"][0]["scanning"] is False
    assert "didn't finish" in overview["keywords"][0]["last_error"]
    assert client.post(reverse("local_visibility:keyword-scan", args=[business.id, keyword_id])).status_code == 202


def test_profile_reach_in_miles(client, business):
    _login(client, "owner-miles@acme.com", account=business.account, role=Membership.Role.OWNER)
    response = client.put(
        reverse("local_visibility:profile", args=[business.id]),
        {"latitude": 29.76, "longitude": -95.36, "grid_size": 5, "radius_miles": 5},
        format="json",
    )
    assert response.status_code == 200
    data = response.json()
    assert data["radius_miles"] == 5.0
    # 5 miles * 1.609344 km/mi = ~8.0467 km
    assert abs(data["radius_km"] - 8.0467) < 0.01

    overview = client.get(reverse("local_visibility:overview", args=[business.id])).json()
    assert overview["profile"]["radius_miles"] == 5.0

