import pytest
from ditare_api.main import limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset rate limiter storage before each test to prevent 429 errors."""
    limiter.reset()
    yield
