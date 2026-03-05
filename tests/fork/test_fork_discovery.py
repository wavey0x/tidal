import os

import pytest


@pytest.mark.skipif(
    os.getenv("ENABLE_FORK_TESTS") != "1",
    reason="Set ENABLE_FORK_TESTS=1 with a fork-capable RPC to run fork tests.",
)
def test_placeholder_fork_discovery() -> None:
    # Real fork tests depend on external infra and are intentionally gated.
    assert True
