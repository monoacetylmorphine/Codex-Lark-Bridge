"""Initialize Git and Office environments per user, preserve existing files, and clean up timed-out subprocesses."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary


# Source: common workflows from four reference skills; the package manager resolves the latest compatible versions on first installation.
PYTHON_PACKAGES: tuple[str, ...] = (
    "defusedxml", "lxml", "Pillow", "openpyxl", "pandas", "pypdf", "pdfplumber",
    "pdf2image", "reportlab", "markitdown[pptx,xlsx]", "pytesseract",
)
NODE_PACKAGE: dict[str, Any] = {
    "name": "office-workspace", "private": True, "version": "1.0.0",
    "engines": {"node": ">=22"},
    "dependencies": {
        name: "latest" for name in ("docx", "pptxgenjs", "react", "react-dom", "react-icons", "sharp")
    },
}
# Source: project wait policy for initial dependency downloads; Git retains its configured timeout.
ENVIRONMENT_TIMEOUT = 600
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_MANIFESTS: tuple[str, ...] = ("package.json", "pnpm-lock.yaml")
_PYTHON_MODULES: tuple[str, ...] = ("defusedxml", "lxml", "PIL", "openpyxl", "pandas", "pypdf", "pdfplumber",
                   "pdf2image", "reportlab", "markitdown", "pytesseract")
_PYTHON_IMPORTS = "import " + ",".join(_PYTHON_MODULES)


def workspace_environment(workspace: Path) -> dict[str, str]:
    """仅返回当前任务覆盖值，不修改服务的全局环境变量。"""
    workspace = workspace.resolve()
    return {
        "VIRTUAL_ENV": str(workspace / ".venv"),
        "PATH": os.pathsep.join((str(workspace / ".venv/bin"),
                                str(workspace / "node_modules/.bin"), os.environ.get("PATH", ""))),
    }


@asynccontextmanager
async def _workspace_lock(workspace: Path) -> AsyncIterator[None]:
    """为工作区同时获取进程内异步锁和跨进程文件锁，在上下文退出时释放。

    工作区目录须已存在；文件锁使用非阻塞重试，不阻塞事件循环。
    """
    # Weak references avoid retaining asyncio.Lock objects as user counts grow; flock coordinates service processes.
    lock = _locks.setdefault(str(workspace), asyncio.Lock())
    async with lock:
        with (workspace / ".office-init.lock").open("a") as handle:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """向独立子进程组发送终止信号，短暂等待后升级为强制终止，并回收管道及子进程。"""
    # Use a separate subprocess session so descendants started by uv/pnpm also receive termination signals.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=2)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def _initialize_git(workspace: Path, timeout: float) -> None:
    """在指定工作区以 main 为初始分支执行 git init，遵守传入的超时。"""
    await _run(["git", "init", "-q", "-b", "main"], workspace, timeout)


async def _run(
    command: list[str],
    workspace: Path,
    timeout: float,
    *,
    env: dict[str, str] | None = None,
) -> bytes:
    """在工作区的独立进程会话中执行命令，成功返回 stdout 字节。

    非零退出码抛出 CalledProcessError，超时或取消时接管可能迟到的子进程并清理进程组，
    待清理完成后重新抛出原异常；env 为 None 时继承服务环境。
    """
    creating = None
    process = None
    try:
        async with asyncio.timeout(timeout):
            creating = asyncio.create_task(asyncio.create_subprocess_exec(
                *command, cwd=workspace, env=env, start_new_session=True,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            ))
            # Cancellation during creation must not lose a process handle returned later.
            process = await asyncio.shield(creating)
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            return stdout
    except BaseException:
        async def cleanup() -> None:
            """等待可能尚未创建完成的子进程，并将其交给进程组回收逻辑处理。"""
            child = process
            if child is None and creating is not None:
                try:
                    child = await creating
                except Exception:
                    return
            if child is not None:
                await _reap(child)
        cleaning = asyncio.create_task(cleanup())
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                continue
        cleaning.result()
        raise


def _write_state(workspace: Path, state: dict[str, Any]) -> None:
    """先写临时 JSON 文件，再原子替换工作区的环境状态文件。"""
    temporary = workspace / ".office-env.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(workspace / ".office-env.json")


def _check_environment_conflicts(
    workspace: Path,
    state: dict[str, Any] | None,
    manifests: dict[str, bytes],
) -> None:
    """只校验受管状态和用户修改，不写入或覆盖工作区。"""
    if state is None:
        conflicts = [name for name in (".venv", "node_modules", *_MANIFESTS, "package-lock.json",
                                       "yarn.lock", "pnpm-workspace.yaml", ".npmrc")
                     if (workspace / name).exists() or (workspace / name).is_symlink()]
        if conflicts:
            raise RuntimeError("The workspace contains an unmanaged environment; nothing was overwritten: " + ", ".join(conflicts))
    elif state.get("owner") != "office":
        raise RuntimeError("Unrecognized workspace .office-env.json; the existing environment was not modified")

    lockfile = workspace / "pnpm-lock.yaml"
    if lockfile.exists():
        recorded = (state or {}).get("manifests", {}).get("pnpm-lock.yaml")
        if recorded and hashlib.sha256(lockfile.read_bytes()).hexdigest() != recorded:
            raise RuntimeError("Workspace pnpm-lock.yaml has been modified and was not overwritten")

    # Upgrade only managed manifests that still match the recorded state; user-modified manifests require manual intervention first.
    for name, content in manifests.items():
        target = workspace / name
        if target.exists() and target.read_bytes() != content:
            recorded = (state or {}).get("manifests", {}).get(name)
            if hashlib.sha256(target.read_bytes()).hexdigest() != recorded:
                raise RuntimeError(f"Workspace {name} has been modified and was not overwritten")

async def _initialize_environment(workspace: Path) -> None:
    """校验或初始化当前用户的 Python 与 Node Office 环境，并记录安装状态及清单摘要。

    已就绪且指纹、清单及依赖文件检查通过时直接复用；拒绝覆盖非受管环境和用户修改。
    需要安装时调用 uv 与 pnpm，完成导入和运行检查后标记 ready；异常向调用方传播，
    保留安装过程状态供后续重试。本函数须在工作区锁内调用。
    """
    manifests = {"package.json": (json.dumps(NODE_PACKAGE, indent=2) + "\n").encode()}
    requirements = "\n".join(PYTHON_PACKAGES).encode()
    node_version = (await _run(["node", "--version"], workspace, 30)).decode().strip()
    fingerprint = hashlib.sha256(requirements + b"".join(manifests.values()) + node_version.encode() +
                                 str((sys.version, platform.system(), platform.machine())).encode()).hexdigest()
    state_path = workspace / ".office-env.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    packages = json.loads(manifests["package.json"])["dependencies"]
    python = workspace / ".venv/bin/python"
    site_packages = workspace / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    if (state and state.get("owner") == "office" and state.get("status") == "ready"
            and state.get("fingerprint") == fingerprint
            and python.is_file() and (workspace / ".venv/pyvenv.cfg").is_file()
            and all((site_packages / name).exists() for name in _PYTHON_MODULES)
            and all((workspace / "node_modules" / package).exists() for package in packages)
            and (workspace / "pnpm-lock.yaml").is_file()
            and hashlib.sha256((workspace / "pnpm-lock.yaml").read_bytes()).hexdigest()
                == state.get("manifests", {}).get("pnpm-lock.yaml")
            and all((workspace / name).is_file() and (workspace / name).read_bytes() == content
                    for name, content in manifests.items())):
        return

    _check_environment_conflicts(workspace, state, manifests)
    lockfile = workspace / "pnpm-lock.yaml"
    if python.exists():
        actual = await _run([str(python), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                            workspace, 30)
        if actual.decode().strip() != f"{sys.version_info.major}.{sys.version_info.minor}":
            raise RuntimeError("The workspace Python version differs from the service version; migrate .venv manually. The environment was not deleted")
    elif (workspace / ".venv").exists():
        raise RuntimeError("Workspace .venv is incomplete and requires manual inspection; it was not rebuilt or deleted")

    required = ("uv", "node", "pnpm", "soffice", "pdftoppm", "pdfinfo", "pdftotext", "pdfimages", "pandoc", "tesseract")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise RuntimeError("Missing system tools: " + ", ".join(missing))
    if int(node_version.lstrip("v").split(".")[0]) < 22:
        raise RuntimeError("The Office workspace requires Node.js >=22")

    state = {"owner": "office", "status": "installing", "fingerprint": fingerprint,
             "manifests": (state or {}).get("manifests", {})}
    _write_state(workspace, state)
    # Use an explicit Python path during installation so uv does not use the service virtual environment.
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_NO_CACHE"] = "false"
    env["CI"] = "true"
    if not python.exists():
        await _run(["uv", "venv", "--python", sys._base_executable, str(workspace / ".venv")],
                   workspace, ENVIRONMENT_TIMEOUT, env=env)
    await _run(["uv", "pip", "install", "--python", str(python), *PYTHON_PACKAGES],
               workspace, ENVIRONMENT_TIMEOUT, env=env)
    for name, content in manifests.items():
        (workspace / name).write_bytes(content)
    state["manifests"].update({name: hashlib.sha256(content).hexdigest() for name, content in manifests.items()})
    _write_state(workspace, state)
    # Generate a user-specific lockfile initially; pnpm reuses existing locks and may update them when direct dependencies are upgraded.
    try:
        await _run(["pnpm", "install", "--no-frozen-lockfile", "--ignore-scripts", "--strict-peer-dependencies"],
                   workspace, ENVIRONMENT_TIMEOUT, env=env)
    finally:
        # Failed installation may still update the lockfile; record installer writes for the next retry.
        if lockfile.is_file():
            state["manifests"]["pnpm-lock.yaml"] = hashlib.sha256(lockfile.read_bytes()).hexdigest()
            _write_state(workspace, state)
    await _run([str(python), "-c", _PYTHON_IMPORTS], workspace, 30, env=env)
    await _run(["uv", "pip", "check", "--python", str(python)], workspace, 30, env=env)
    # Execute sharp to verify its platform binary rather than merely checking for the package directory.
    node_probe = """const fs = require('node:fs');
for (const name of Object.keys(JSON.parse(fs.readFileSync('package.json')).dependencies)) require(name);
require('sharp')(Buffer.from('<svg width="8" height="8"><rect width="8" height="8"/></svg>'))
  .png().toBuffer().catch(e => { console.error(e); process.exitCode = 1; });"""
    await _run(["node", "-e", node_probe], workspace, 30, env=env)
    state["status"] = "ready"
    state["node"] = node_version
    _write_state(workspace, state)


async def prepare_workspace(workspace: Path, timeout: float) -> None:
    """创建用户工作区，保留已有 AGENTS.md，并在锁内初始化缺失的 Git 仓库及 Office 环境。

    传入的 timeout 仅用于 Git 初始化；环境初始化及等待锁另受 ENVIRONMENT_TIMEOUT 限制。
    已有 .git 文件或目录均视为已初始化，依赖检查或安装失败向调用方传播。
    """
    workspace = workspace.resolve()
    def prepare() -> None:
        """创建工作区目录，仅在 AGENTS.md 不存在时创建空文件，不覆盖已有内容。"""
        workspace.mkdir(parents=True, exist_ok=True)
        try:
            (workspace / "AGENTS.md").touch(exist_ok=False)
        except FileExistsError:
            pass

    await asyncio.to_thread(prepare)
    async with asyncio.timeout(ENVIRONMENT_TIMEOUT):
        async with _workspace_lock(workspace):
            # A worktree .git file also indicates initialization; recheck after acquiring the lock.
            if not (workspace / ".git").exists():
                await _initialize_git(workspace, timeout)
            await _initialize_environment(workspace)
