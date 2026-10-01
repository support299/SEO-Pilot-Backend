"""Fetches one site and turns the HTML it actually downloaded into findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

PAGE_LIMIT = 25
USER_AGENT = "SEOPilotBot/1.0"
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".pdf", ".zip", ".mp4", ".woff", ".woff2")


@dataclass
class ParsedPage:
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    noindex: bool = False
    links: list[str] = field(default_factory=list)


@dataclass
class FetchedPage:
    url: str
    status_code: int
    content_type: str
    title: str
    meta_description: str
    canonical: str
    noindex: bool
    is_html: bool
    error: str
    links: list[str]


@dataclass
class CrawlFinding:
    url: str
    category: str
    severity: str
    title: str
    detail: str


class _HtmlExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title_parts: list[str] = []
        self.page = ParsedPage()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            content = attributes.get("content", "")
            if name in {"description", "og:description"} and not self.page.meta_description:
                self.page.meta_description = " ".join(content.split())
            if name == "robots" and "noindex" in content.lower():
                self.page.noindex = True
        elif tag == "link" and "canonical" in attributes.get("rel", "").lower():
            self.page.canonical = attributes.get("href", "").strip()
        elif tag == "a" and attributes.get("href"):
            self.page.links.append(attributes["href"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    def finish(self) -> ParsedPage:
        self.page.title = " ".join("".join(self._title_parts).split())
        return self.page


def parse_html(html: str) -> ParsedPage:
    extractor = _HtmlExtractor()
    extractor.feed(html)
    return extractor.finish()


def host_key(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def normalize_url(url: str) -> str:
    url, _fragment = urldefrag(url.strip())
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return parsed._replace(path=path, fragment="").geturl()


def same_site(url: str, seed: str) -> bool:
    return host_key(url) == host_key(seed) and urlparse(url).scheme in {"http", "https"}


def _skip_link(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(extension) for extension in SKIP_EXTENSIONS)


def score_findings(findings: list[CrawlFinding]) -> int:
    """100 minus a fixed penalty per finding. This is not a Google score."""
    penalties = {"critical": 20, "high": 10, "medium": 4}
    penalty = sum(penalties.get(finding.severity, 0) for finding in findings)
    return max(0, 100 - penalty)


def build_findings(pages: list[FetchedPage]) -> list[CrawlFinding]:
    findings: list[CrawlFinding] = []
    html_pages = [page for page in pages if page.is_html and page.status_code and page.status_code < 400 and not page.error]

    for page in pages:
        if page.error or page.status_code == 0 or page.status_code >= 400:
            findings.append(
                CrawlFinding(
                    url=page.url,
                    category="Reachability",
                    severity="critical",
                    title="Page did not load",
                    detail=page.error or f"HTTP {page.status_code}",
                )
            )
            continue
        if not page.is_html:
            continue
        if not page.title:
            findings.append(CrawlFinding(url=page.url, category="Titles", severity="high", title="Missing title", detail="The HTML response has no <title>."))
        if not page.meta_description:
            findings.append(CrawlFinding(url=page.url, category="Descriptions", severity="medium", title="Missing meta description", detail="The HTML response has no meta description."))
        if page.noindex:
            findings.append(
                CrawlFinding(
                    url=page.url,
                    category="Indexing hints",
                    severity="medium",
                    title="Page asks not to be indexed",
                    detail="A noindex robots tag was in the HTML. This is not a Google index-coverage check.",
                )
            )

    titles: dict[str, list[str]] = {}
    descriptions: dict[str, list[str]] = {}
    for page in html_pages:
        if page.title:
            titles.setdefault(page.title.casefold(), []).append(page.url)
        if page.meta_description:
            descriptions.setdefault(page.meta_description.casefold(), []).append(page.url)

    for urls in titles.values():
        if len(urls) < 2:
            continue
        for url in urls:
            findings.append(CrawlFinding(url=url, category="Titles", severity="medium", title="Duplicate title", detail=f"This title is shared with {len(urls) - 1} other crawled page(s)."))
    for urls in descriptions.values():
        if len(urls) < 2:
            continue
        for url in urls:
            findings.append(CrawlFinding(url=url, category="Descriptions", severity="medium", title="Duplicate meta description", detail=f"This description is shared with {len(urls) - 1} other crawled page(s)."))

    return findings


def category_summaries(pages: list[FetchedPage], findings: list[CrawlFinding]) -> list[dict]:
    def count(category: str) -> int:
        return sum(1 for finding in findings if finding.category == category)

    html_count = sum(1 for page in pages if page.is_html and page.status_code and page.status_code < 400 and not page.error)
    reachability = count("Reachability")
    titles = count("Titles")
    descriptions = count("Descriptions")
    noindex = sum(1 for page in pages if page.noindex)
    return [
        {
            "label": "Reachability",
            "status": "Healthy" if reachability == 0 else "Needs work",
            "summary": "Every crawled URL loaded." if reachability == 0 else f"{reachability} crawled URL(s) did not load.",
        },
        {
            "label": "Titles",
            "status": "Healthy" if titles == 0 else "Needs work",
            "summary": f"{html_count} HTML page(s) have a unique title." if titles == 0 else f"{titles} title finding(s) in the crawled HTML.",
        },
        {
            "label": "Descriptions",
            "status": "Healthy" if descriptions == 0 else "Needs work",
            "summary": "Crawled HTML pages have unique meta descriptions." if descriptions == 0 else f"{descriptions} description finding(s) in the crawled HTML.",
        },
        {
            "label": "Indexing hints",
            "status": "From the crawl",
            "summary": f"{noindex} crawled page(s) send noindex. This is not Google's index count.",
        },
    ]


class SiteCrawler:
    def __init__(self, seed_url: str, *, limit: int = PAGE_LIMIT, session: requests.Session | None = None) -> None:
        self.seed_url = normalize_url(seed_url)
        self.limit = limit
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def crawl(self) -> list[FetchedPage]:
        robots = self._robots()
        queue = self._seed_urls(robots)
        seen: set[str] = set()
        pages: list[FetchedPage] = []

        while queue and len(pages) < self.limit:
            url = queue.pop(0)
            if url in seen or not same_site(url, self.seed_url) or _skip_link(url):
                continue
            if robots is not None and not robots.can_fetch(USER_AGENT, url):
                continue
            seen.add(url)
            fetched = self._fetch(url)
            pages.append(fetched)
            for link in fetched.links:
                absolute = normalize_url(urljoin(fetched.url, link))
                if absolute not in seen and same_site(absolute, self.seed_url) and not _skip_link(absolute):
                    queue.append(absolute)
        return pages

    def _robots(self) -> RobotFileParser | None:
        parsed = urlparse(self.seed_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            response = self.session.get(robots_url, timeout=12)
        except requests.RequestException:
            return None
        if not response.ok:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    def _seed_urls(self, robots: RobotFileParser | None) -> list[str]:
        urls = [self.seed_url]
        parsed = urlparse(self.seed_url)
        sitemap_url = f"{parsed.scheme}://{parsed.netloc}/sitemap.xml"
        if robots is None or robots.can_fetch(USER_AGENT, sitemap_url):
            urls.extend(self._sitemap_urls(sitemap_url, depth=0))
        deduped: list[str] = []
        for url in urls:
            if url not in deduped:
                deduped.append(url)
        return deduped[: self.limit * 4]

    def _sitemap_urls(self, sitemap_url: str, depth: int) -> list[str]:
        if depth > 1:
            return []
        try:
            response = self.session.get(sitemap_url, timeout=12)
        except requests.RequestException:
            return []
        if not response.ok or "<loc>" not in response.text.lower():
            return []
        found: list[str] = []
        for raw in _loc_values(response.text):
            url = normalize_url(raw)
            if not same_site(url, self.seed_url):
                continue
            if url.endswith(".xml") and depth < 1:
                found.extend(self._sitemap_urls(url, depth + 1))
            else:
                found.append(url)
            if len(found) >= self.limit:
                break
        return found

    def _fetch(self, url: str) -> FetchedPage:
        try:
            response = self.session.get(url, timeout=12, allow_redirects=True)
        except requests.RequestException as exc:
            return FetchedPage(url, 0, "", "", "", "", False, False, str(exc), [])

        final_url = normalize_url(response.url)
        content_type = response.headers.get("Content-Type", "")
        is_html = "html" in content_type.lower() or response.text.lstrip()[:1] == "<"
        if not same_site(final_url, self.seed_url):
            return FetchedPage(url, response.status_code, content_type, "", "", "", False, False, "Redirected off the business site.", [])

        parsed = parse_html(response.text) if is_html and response.ok else ParsedPage()
        return FetchedPage(
            url=final_url,
            status_code=response.status_code,
            content_type=content_type.split(";")[0],
            title=parsed.title,
            meta_description=parsed.meta_description,
            canonical=parsed.canonical,
            noindex=parsed.noindex,
            is_html=is_html and response.ok,
            error="" if response.ok else f"HTTP {response.status_code}",
            links=parsed.links if is_html and response.ok else [],
        )


def _loc_values(xml: str) -> list[str]:
    values: list[str] = []
    lowered = xml
    start = 0
    while True:
        open_at = lowered.lower().find("<loc>", start)
        if open_at < 0:
            break
        close_at = lowered.lower().find("</loc>", open_at)
        if close_at < 0:
            break
        values.append(lowered[open_at + 5 : close_at].strip())
        start = close_at + 6
    return values
