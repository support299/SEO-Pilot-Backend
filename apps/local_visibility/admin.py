from django.contrib import admin

from .models import GridPoint, GridScan, LocalVisibilityProfile, TrackedKeyword

admin.site.register(LocalVisibilityProfile)
admin.site.register(TrackedKeyword)
admin.site.register(GridScan)
admin.site.register(GridPoint)
