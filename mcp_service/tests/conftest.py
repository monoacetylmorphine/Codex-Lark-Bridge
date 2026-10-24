from __future__ import annotations

from pathlib import Path
import sys
from contextlib import ExitStack
import pytest
import pytest_asyncio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    instance = getattr(item, "instance", None)
    if instance is not None and hasattr(instance, "_pytest_stack"):
        instance._pytest_stack.close()


@pytest_asyncio.fixture(autouse=True)
async def _pytest_case_cleanup(request):
    instance = getattr(request.node, "instance", None)
    if instance is not None:
        instance._pytest_stack = ExitStack()
        instance._pytest_async_cleanups = []
    yield
    instance = getattr(request.node, "instance", None)
    if instance is not None:
        for function in reversed(instance._pytest_async_cleanups):
            await function()
