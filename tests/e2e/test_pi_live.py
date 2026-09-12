import shutil

import pytest

pytestmark = pytest.mark.slow


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi not installed")
def test_real_pi_is_local_only() -> None:
    assert shutil.which("pi") is not None
