from django.db import migrations


def backfill_crawls(apps, schema_editor):
    CrawlRun = apps.get_model("site_health", "CrawlRun")
    Finding = apps.get_model("site_health", "Finding")
    Event = apps.get_model("history", "Event")

    for crawl in CrawlRun.objects.filter(status__in=["completed", "failed"]):
        when = crawl.finished_at or crawl.created_at
        if crawl.status == "completed":
            findings = Finding.objects.filter(crawl=crawl).count()
            summary = f"Site crawl completed: score {crawl.score}, {crawl.pages_crawled} pages, {findings} findings."
            kind, metadata = "crawl.completed", {"score": crawl.score, "pages_crawled": crawl.pages_crawled, "findings": findings}
        else:
            summary = f"Site crawl failed: {crawl.error or 'unknown error'}"
            kind, metadata = "crawl.failed", {}
        Event.objects.create(business_id=crawl.business_id, kind=kind, summary=summary[:500], metadata=metadata, occurred_at=when)


class Migration(migrations.Migration):

    dependencies = [
        ("history", "0001_initial"),
        ("site_health", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill_crawls, migrations.RunPython.noop)]
