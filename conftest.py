import pytest


@pytest.fixture(autouse=True)
def _clear_cache():
    """
    DRF's rate limiting (ScopedRateThrottle) uses Django's default cache,
    which — unlike the test database — isn't reset between tests. Without
    this, registering/logging in many users across a full test run eventually
    trips the "auth" throttle scope, failing unrelated tests with 429s that
    have nothing to do with what they're actually testing.
    """
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
