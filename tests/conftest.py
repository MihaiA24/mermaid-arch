from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from mermaid_arch.browser import BrowserSession
from mermaid_arch.models import DiagramError


@pytest.fixture(scope="session")
def browser_session() -> Iterator[BrowserSession]:
    """One headless Chromium for the whole run; tests needing it skip when none can launch.

    With MERMAID_ARCH_REQUIRE_BROWSER=1, as in CI, they fail instead: a skip there would pass
    a run that rendered nothing.
    """
    with BrowserSession() as session:
        try:
            session.describe()
        except DiagramError as exc:
            if os.environ.get("MERMAID_ARCH_REQUIRE_BROWSER") == "1":
                pytest.fail(str(exc))
            pytest.skip(str(exc))
        yield session


@pytest.fixture
def in_own_thread(browser_session: BrowserSession) -> Iterator[Callable[..., Any]]:
    """Calls `function(*args)` for code that opens its own BrowserSession: `main`, `run_doctor`.

    Playwright's sync API runs once per thread, and `browser_session` holds this thread's for
    the whole run. Skips, like `browser_session`, when no Chromium can launch.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        yield lambda function, *args: pool.submit(function, *args).result()
