# -*- coding: utf-8 -*-
"""Prepare shared skill dependencies whenever the Hermes gateway starts.

Installations run in a daemon thread so a slow package index never prevents the
gateway from starting.  Progress and failures are written to the Hermes log.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterable, Optional


logger = logging.getLogger("hooks.global-env-checking")

PYTHON_VERSION = os.getenv("SKILL_PYTHON_VERSION", "3.13")
COMMAND_TIMEOUT = int(os.getenv("ENV_CHECK_TIMEOUT_MS", "120000")) / 1000
QUICK_CHECK_TIMEOUT = 10
LOCK_PATH = Path(os.getenv("ENV_CHECK_LOCK", str(Path(tempfile.gettempdir()) / "global-env-check.lock")))
LOCK_STALE_SECONDS = 30 * 60

# The default matches the Docker bind mount ~/.hermes/workspace:/opt/data/workspace.
# Override these paths only when Hermes uses a different volume layout.
VENV_ROOT = Path(os.getenv("ENV_CHECK_VENV_ROOT", "/opt/data/workspace"))
# gateway:startup has no workspace context. Do not fall back to the gateway's
# process cwd: doing so could create package.json/node_modules beside Hermes.
WORKSPACE_DIR = Path(os.getenv("ENV_CHECK_WORKSPACE_DIR", "/opt/data/workspace"))

# Langfuse is a Hermes observability plugin dependency, not a skill dependency.
# The plugin runs in the Gateway process, so install it in the Gateway's venv
# rather than one of the workspace skill environments.
HERMES_VENV = Path(os.getenv("ENV_CHECK_HERMES_VENV", "/opt/hermes/.venv"))
LANGFUSE_PACKAGES = ("langfuse",)

SKILL_ENVS = (
    ("asr", ("av", "python-dotenv", "openai", "numpy")),
    ("iti", ("aiohttp", "aiofiles", "python-dotenv", "openai", "lark-oapi")),
    ("pdf", ("pypdf", "pdfplumber", "reportlab", "pytesseract", "pdf2image", "pandas", "openpyxl")),
    ("ppt", ("markitdown[pptx]", "Pillow", "defusedxml", "lxml")),
    ("video", ("aiohttp", "aiofiles", "python-dotenv", "openai", "lark-oapi", "volcengine-python-sdk[ark]")),
    ("tti", ("aiohttp", "aiofiles", "python-dotenv", "openai", "lark-oapi")),
    ("xlsx", ("openpyxl", "pandas", "markitdown")),
)
WORKSPACE_NODE_MODULES = ("docx",)
SKILL_NODE_MODULES = {
    "pdf": ("pdf-lib", "pdf-parse"),
    "ppt": ("pptxgenjs", "sharp", "react-icons", "react", "react-dom"),
}
SYSTEM_COMMANDS = ("zip", "unzip")


def _run(
    command: list[str], *, cwd: Optional[Path] = None, timeout: float = COMMAND_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell and include its output in failures."""
    logger.info("running: %s", " ".join(command))
    return subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def _resolve_uv() -> str | None:
    configured = os.getenv("UV_BINARY")
    if configured and os.access(configured, os.X_OK):
        return configured

    discovered = shutil.which("uv")
    if discovered:
        return discovered

    home = Path.home()
    for candidate in (home / ".local/bin/uv", home / ".cargo/bin/uv", Path("/usr/local/bin/uv"), Path("/usr/bin/uv")):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _ensure_uv() -> str | None:
    uv = _resolve_uv()
    if uv:
        return uv

    # Automatic bootstrap remains opt-in because it downloads and executes an
    # external installer.  Set ENV_CHECK_INSTALL_UV=1 on trusted deployments.
    if os.getenv("ENV_CHECK_INSTALL_UV") != "1":
        logger.warning("uv is unavailable; install it or set ENV_CHECK_INSTALL_UV=1")
        return None

    installer = Path(tempfile.gettempdir()) / "hermes-uv-install.sh"
    install_dir = Path(os.getenv("UV_INSTALL_DIR", str(Path.home() / ".local/bin")))
    try:
        install_dir.mkdir(parents=True, exist_ok=True)
        _run(["curl", "--fail", "--silent", "--show-error", "--location", "https://astral.sh/uv/install.sh", "--output", str(installer)], timeout=60)
        environment = os.environ.copy()
        environment["UV_INSTALL_DIR"] = str(install_dir)
        subprocess.run(["sh", str(installer)], check=True, timeout=COMMAND_TIMEOUT, env=environment)
        return _resolve_uv()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("could not install uv: %s", exc)
        return None
    finally:
        installer.unlink(missing_ok=True)


def _ensure_venv(name: str, packages: Iterable[str], uv: str) -> None:
    venv_path = VENV_ROOT / f"venv_{name}"
    python = venv_path / "bin/python"
    try:
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(python, os.X_OK):
            _run([uv, "venv", "--python", PYTHON_VERSION, "--seed", "--allow-existing", str(venv_path)])
        _run([uv, "pip", "install", "--python", str(python), "--upgrade", *packages])
        logger.info("%s venv ready", name)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("%s venv setup failed: %s", name, exc)


def _ensure_hermes_packages(packages: Iterable[str], uv: str) -> None:
    """Install Gateway-plugin dependencies in Hermes's own virtualenv."""
    python = HERMES_VENV / "bin/python"
    if not os.access(python, os.X_OK):
        logger.warning("Hermes Python is unavailable at %s; cannot install %s", python, ", ".join(packages))
        return
    try:
        # This hook runs as the Gateway user.  Avoid unconditionally replacing
        # already-installed dependencies (which may have been added by a
        # root-owned image layer) on every Gateway startup.
        _run([uv, "pip", "install", "--python", str(python), *packages])
        logger.info("Hermes Gateway packages ready: %s", ", ".join(packages))
    except subprocess.CalledProcessError as exc:
        # CalledProcessError.__str__ only contains the exit code.  Preserve uv's
        # output so startup logs identify issues such as DNS/auth failures,
        # invalid Python paths, or incompatible package versions.
        logger.error(
            "Hermes Gateway package setup failed (exit %s): stdout=%s stderr=%s",
            exc.returncode,
            exc.stdout.strip() if exc.stdout else "<empty>",
            exc.stderr.strip() if exc.stderr else "<empty>",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Hermes Gateway package setup failed: %s", exc)


def _ensure_node_modules(modules: Iterable[str]) -> None:
    modules = tuple(modules)
    if not modules:
        return
    if not _command_exists("node") or not _command_exists("npm"):
        logger.warning("Node/npm unavailable; cannot check %s", ", ".join(modules))
        return
    try:
        WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        missing = []
        for module in modules:
            try:
                _run(["node", "-e", "require.resolve(process.argv[1])", module], cwd=WORKSPACE_DIR, timeout=QUICK_CHECK_TIMEOUT)
            except subprocess.SubprocessError:
                missing.append(module)
        if missing:
            _run(["npm", "install", "--no-audit", "--no-fund", "--prefer-offline", *missing], cwd=WORKSPACE_DIR)
            logger.info("installed Node modules: %s", ", ".join(missing))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("Node module setup failed: %s", exc)


def _acquire_lock() -> bool:
    try:
        LOCK_PATH.mkdir()
        (LOCK_PATH / "owner").write_text(f"{os.getpid()}\\n{time.time()}\\n", encoding="utf-8")
        return True
    except FileExistsError:
        try:
            if time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE_SECONDS:
                shutil.rmtree(LOCK_PATH)
                return _acquire_lock()
        except FileNotFoundError:
            return _acquire_lock()
        return False


def _check_environment() -> None:
    if not _acquire_lock():
        logger.info("environment setup is already running; skipping duplicate startup hook")
        return
    try:
        missing_system = [command for command in SYSTEM_COMMANDS if not _command_exists(command)]
        if missing_system:
            logger.warning("missing system commands (not installed automatically): %s", ", ".join(missing_system))

        _ensure_node_modules(WORKSPACE_NODE_MODULES)
        uv = _ensure_uv()
        if uv:
            _ensure_hermes_packages(LANGFUSE_PACKAGES, uv)
            for name, packages in SKILL_ENVS:
                _ensure_venv(name, packages, uv)
        _ensure_node_modules(module for modules in SKILL_NODE_MODULES.values() for module in modules)
        logger.info("environment check completed")
    except Exception:
        logger.exception("unexpected environment-check failure")
    finally:
        shutil.rmtree(LOCK_PATH, ignore_errors=True)


async def handle(event_type: str, context: dict) -> None:
    """Handle Hermes ``gateway:startup`` without delaying gateway readiness."""
    if event_type != "gateway:startup":
        return
    logger.info("starting global environment check")
    threading.Thread(target=_check_environment, name="global-env-checking", daemon=True).start()
