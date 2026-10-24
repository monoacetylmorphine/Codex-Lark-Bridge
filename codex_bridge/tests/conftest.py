from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parent

# Load the checked-out source when tests run inside the runtime image.
sys.path.insert(0, str(REPOSITORY_ROOT))

_temporary = tempfile.TemporaryDirectory(prefix="codex_bridge-pytest-")
_settings = json.loads(
    (PROJECT_ROOT / "configs/config_default.json").read_text(encoding="utf-8")
)
_settings["storage"].update(
    databasePath=str(Path(_temporary.name) / "state.sqlite"),
    workspaceRoot=str(Path(_temporary.name) / "workspace"),
)
_config_path = Path(_temporary.name) / "config.json"
_config_path.write_text(json.dumps(_settings), encoding="utf-8")
_previous_config_file = os.environ.get("CONFIG_FILE")
os.environ["CONFIG_FILE"] = str(_config_path)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--reverse",
        action="store_true",
        default=False,
        help="run collected tests in reverse order",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--reverse"):
        items.reverse()


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
        if hasattr(instance, "asyncTearDown"):
            await instance.asyncTearDown()


@pytest.hookimpl
def pytest_configure(config: pytest.Config) -> None:
    dotenv = patch("dotenv.load_dotenv")
    dotenv.start()
    config._codex_bridge_test_patches = (dotenv,)


@pytest.hookimpl
def pytest_unconfigure(config: pytest.Config) -> None:
    for patcher in getattr(config, "_codex_bridge_test_patches", ()):
        patcher.stop()
    if _previous_config_file is None:
        os.environ.pop("CONFIG_FILE", None)
    else:
        os.environ["CONFIG_FILE"] = _previous_config_file
    _temporary.cleanup()
