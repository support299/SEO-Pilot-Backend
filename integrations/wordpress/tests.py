from integrations.wordpress import client as wp

AUTH = ("admin", "app-password")
API = "https://example.com/wp-json"


class FakeWordPress:
    """Records every request and answers like a site running All in One SEO."""

    def __init__(self, aioseo=None, core_writes_aioseo=True, meta=None):
        self.aioseo = aioseo  # None = site has no AIOSEO
        self.core_writes_aioseo = core_writes_aioseo
        self.meta = meta or {}
        self.calls = []

    def __call__(self, api_url, route, *, username=None, application_password=None, method="GET", body=None, timeout=10):
        self.calls.append((method, route, body))
        if route.startswith("/aioseo/v1/post"):
            self.aioseo.update({k: v for k, v in body.items() if k in ("title", "description")})
            return {"success": True}
        if method == "POST":
            if self.aioseo is not None and self.core_writes_aioseo:
                self.aioseo.update(body.get("aioseo_meta_data", {}))
            return self._object(body.get("title"))
        if "_fields=aioseo_meta_data" in route:
            return {"aioseo_meta_data": self.aioseo} if self.aioseo is not None else {}
        return self._object()

    def _object(self, title=None):
        payload = {"id": 5, "title": {"raw": title or "Blog"}, "slug": "blog", "status": "publish", "link": "https://example.com/blog/", "modified_gmt": "x", "meta": self.meta}
        if self.aioseo is not None:
            payload["aioseo_meta_data"] = self.aioseo
        return payload

    def writes(self):
        return [(route, body) for method, route, body in self.calls if method == "POST"]


def _update(fake, monkeypatch, changes):
    monkeypatch.setattr(wp, "wp_fetch_json", fake)
    return wp.update_object(API, "page", 5, changes, *AUTH)


class TestAioseoWrites:
    def test_title_goes_to_seo_title_and_leaves_page_title_alone(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "Old SEO title", "description": "Old"})
        _update(fake, monkeypatch, {"title": "New SEO title"})

        (route, body), = fake.writes()
        assert route == "/wp/v2/pages/5"
        assert body == {"aioseo_meta_data": {"title": "New SEO title"}}
        assert "title" not in body
        assert fake.aioseo["title"] == "New SEO title"

    def test_title_and_description_written_together(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "T", "description": "D"})
        _update(fake, monkeypatch, {"title": "New T", "meta_description": "New D"})

        assert fake.writes() == [("/wp/v2/pages/5", {"aioseo_meta_data": {"title": "New T", "description": "New D"}})]

    def test_empty_value_is_written_to_clear_the_field(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "T", "description": "D"})
        _update(fake, monkeypatch, {"title": "", "meta_description": ""})

        assert fake.aioseo == {"title": "", "description": ""}

    def test_falls_back_to_aioseo_endpoint_with_full_record(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "T", "description": "D", "robots_noindex": False}, core_writes_aioseo=False)
        _update(fake, monkeypatch, {"meta_description": "New D"})

        route, body = fake.writes()[-1]
        assert route == "/aioseo/v1/post"
        assert body == {"title": "T", "description": "New D", "robots_noindex": False, "id": 5}
        assert fake.aioseo["description"] == "New D"

    def test_no_fallback_when_core_write_landed(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "T", "description": "D"})
        _update(fake, monkeypatch, {"meta_description": "New D"})

        assert all(route != "/aioseo/v1/post" for route, _ in fake.writes())

    def test_html_escaped_stored_value_counts_as_landed(self, monkeypatch):
        fake = FakeWordPress(aioseo={"title": "T", "description": "D"})
        monkeypatch.setattr(wp, "wp_fetch_json", fake)
        original = fake.__call__

        def escaping(api_url, route, **kw):
            result = original(api_url, route, **kw)
            if "_fields=aioseo_meta_data" in route:
                return {"aioseo_meta_data": {**fake.aioseo, "title": fake.aioseo["title"].replace("&", "&amp;")}}
            return result

        monkeypatch.setattr(wp, "wp_fetch_json", escaping)
        wp.update_object(API, "page", 5, {"title": "Tips & Growth"}, *AUTH)

        assert all(route != "/aioseo/v1/post" for route, _ in fake.writes())


class TestOtherPlugins:
    def test_without_aioseo_title_is_the_page_title(self, monkeypatch):
        fake = FakeWordPress(aioseo=None)
        _update(fake, monkeypatch, {"title": "New Title"})

        assert fake.writes() == [("/wp/v2/pages/5", {"title": "New Title"})]

    def test_without_aioseo_description_goes_to_yoast_and_rank_math(self, monkeypatch):
        fake = FakeWordPress(aioseo=None)
        _update(fake, monkeypatch, {"meta_description": "New D"})

        assert fake.writes() == [("/wp/v2/pages/5", {"meta": {"rank_math_description": "New D", "_yoast_wpseo_metadesc": "New D"}})]


class TestReading:
    def test_aioseo_values_are_unescaped_into_meta(self):
        obj = wp._to_wp_object({"id": 5, "title": {"raw": "Blog"}, "aioseo_meta_data": {"title": "Tips &amp; Growth", "description": "A &amp; B"}})

        assert obj.meta["_aioseo_title"] == "Tips & Growth"
        assert obj.meta["_aioseo_description"] == "A & B"

    def test_no_aioseo_keys_when_plugin_absent(self):
        obj = wp._to_wp_object({"id": 5, "title": {"raw": "Blog"}})

        assert "_aioseo_title" not in obj.meta
