from django.contrib import admin

from .models import CrawledPage, CrawlRun, Finding

admin.site.register(CrawlRun)
admin.site.register(CrawledPage)
admin.site.register(Finding)
