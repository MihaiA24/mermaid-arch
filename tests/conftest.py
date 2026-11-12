from __future__ import annotations

from collections.abc import Iterator

import pytest

from mermaid_arch.browser import BrowserSession
from mermaid_arch.models import DiagramError


@pytest.fixture(scope="session")
def browser_session() -> Iterator[BrowserSession]:
    """One headless Chromium for the whole run; tests needing it skip when none can launch."""
    with BrowserSession() as session:
        try:
            session.describe()
        except DiagramError as exc:
            pytest.skip(str(exc))
        yield session
