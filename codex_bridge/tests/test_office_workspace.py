"""Offline verification of user environment initialization, recovery, conflict protection, and task environment isolation."""
from __future__ import annotations
import pytest
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import AsyncMock, patch
from codex_bridge.codex import codex_workspace as module

class OfficeWorkspaceTests:

    def setup_method(self) -> None:
        self.root = Path(self._pytest_stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.workspace = self.root / 'user-a'
        self.workspace.mkdir()
        self.calls = []
        self._pytest_stack.enter_context(patch.object(module.shutil, 'which', lambda name: '/usr/bin/' + name))
        self._pytest_stack.enter_context(patch.object(module, '_run', side_effect=self.run_command))

    async def run_command(self, command, workspace, timeout, **kwargs):
        self.calls.append((command, workspace, kwargs))
        if command == ['node', '--version']:
            return b'v22.20.0\n'
        if command == ['pnpm', '--version']:
            return b'11.19.0\n'
        if command[:2] == ['git', 'init']:
            (workspace / '.git').mkdir()
        if command[:2] == ['uv', 'venv']:
            (workspace / '.venv/bin').mkdir(parents=True)
            (workspace / '.venv/bin/python').touch()
            (workspace / '.venv/pyvenv.cfg').touch()
        if command[:3] == ['uv', 'pip', 'install']:
            site = workspace / f'.venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
            for name in module._PYTHON_MODULES:
                (site / name).mkdir(parents=True, exist_ok=True)
        if command[:2] == ['pnpm', 'install']:
            (workspace / 'pnpm-lock.yaml').write_text("lockfileVersion: '9.0'\n")
            packages = json.loads((workspace / 'package.json').read_text())['dependencies']
            for name in packages:
                (workspace / 'node_modules' / name).mkdir(parents=True, exist_ok=True)
        if 'sys.version_info' in command[-1]:
            return f'{sys.version_info.major}.{sys.version_info.minor}\n'.encode()
        return b''

    async def test_initializes_only_user_directory_and_reuses_environment(self) -> None:
        await module.prepare_workspace(self.workspace, 5)
        state = json.loads((self.workspace / '.office-env.json').read_text())
        assert state['status'] == 'ready'
        assert (self.workspace / '.git').is_dir()
        assert (self.workspace / 'AGENTS.md').is_file()
        assert not (self.root / '.venv').exists()
        assert not (self.root / 'node_modules').exists()
        install = next((args for args, _, _ in self.calls if args[:3] == ['uv', 'pip', 'install']))
        assert str(self.workspace / '.venv/bin/python') in install
        assert set(module.PYTHON_PACKAGES).issubset(install)
        assert '-r' not in install
        assert all(('==' not in package for package in module.PYTHON_PACKAGES))
        assert set(module.NODE_PACKAGE['dependencies'].values()) == {'latest'}
        assert (self.workspace / 'pnpm-lock.yaml').is_file()
        self.calls.clear()
        await module.prepare_workspace(self.workspace, 5)
        assert [args for args, _, _ in self.calls] == [['node', '--version']]

    async def test_existing_git_and_agents_still_get_environment(self) -> None:
        (self.workspace / '.git').write_text('gitdir: /synthetic/worktree')
        (self.workspace / 'AGENTS.md').write_text('Preserve user instructions')
        await module.prepare_workspace(self.workspace, 5)
        assert (self.workspace / 'AGENTS.md').read_text() == 'Preserve user instructions'
        assert (self.workspace / '.venv/bin/python').exists()
        assert not any((args[:2] == ['git', 'init'] for args, _, _ in self.calls))

    async def test_unmanaged_manifest_and_venv_are_not_overwritten(self) -> None:
        for name in ('package.json', '.venv'):
            workspace = self.root / name.replace('.', '')
            workspace.mkdir()
            target = workspace / name
            target.write_text('User file')
            with pytest.raises(RuntimeError, match='unmanaged'):
                await module.prepare_workspace(workspace, 5)
            assert target.read_text() == 'User file'
            assert not (workspace / '.office-env.json').exists()

    async def test_failed_install_is_not_ready_and_can_retry(self) -> None:

        async def fail(command, *args, **kwargs):
            if command[:2] == ['pnpm', 'install']:
                raise subprocess.CalledProcessError(1, command, stderr=b'offline')
            return await self.run_command(command, *args, **kwargs)
        with patch.object(module, '_run', side_effect=fail):
            with pytest.raises(subprocess.CalledProcessError):
                await module.prepare_workspace(self.workspace, 5)
        assert json.loads((self.workspace / '.office-env.json').read_text())['status'] == 'installing'
        await module.prepare_workspace(self.workspace, 5)
        assert json.loads((self.workspace / '.office-env.json').read_text())['status'] == 'ready'

    async def test_modified_managed_manifest_is_preserved(self) -> None:
        await module.prepare_workspace(self.workspace, 5)
        manifest = self.workspace / 'package.json'
        content = manifest.read_text() + '\n'
        manifest.write_text(content)
        with pytest.raises(RuntimeError, match='has been modified'):
            await module.prepare_workspace(self.workspace, 5)
        assert manifest.read_text() == content

    async def test_node_install_failure_after_lock_update_can_retry(self) -> None:
        await module.prepare_workspace(self.workspace, 5)
        (self.workspace / 'node_modules/docx').rmdir()

        async def fail(command, *args, **kwargs):
            if command[:2] == ['pnpm', 'install']:
                (self.workspace / 'pnpm-lock.yaml').write_text('lockfileVersion: 9\n# installer update\n')
                raise subprocess.CalledProcessError(1, command)
            return await self.run_command(command, *args, **kwargs)
        with patch.object(module, '_run', side_effect=fail):
            with pytest.raises(subprocess.CalledProcessError):
                await module.prepare_workspace(self.workspace, 5)
        await module.prepare_workspace(self.workspace, 5)
        assert (self.workspace / 'node_modules/docx').exists()

    async def test_upgrade_download_failure_preserves_manifest_ownership_for_retry(self) -> None:
        await module.prepare_workspace(self.workspace, 5)
        manifest = self.workspace / 'package.json'
        old = json.loads(manifest.read_text())
        old['version'] = '0.9.0'
        manifest.write_text(json.dumps(old))
        state_path = self.workspace / '.office-env.json'
        state = json.loads(state_path.read_text())
        state['fingerprint'] = 'previous-release'
        state['manifests']['package.json'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        state_path.write_text(json.dumps(state))

        async def fail(command, *args, **kwargs):
            if command[:3] == ['uv', 'pip', 'install']:
                raise subprocess.CalledProcessError(1, command)
            return await self.run_command(command, *args, **kwargs)
        with patch.object(module, '_run', side_effect=fail):
            with pytest.raises(subprocess.CalledProcessError):
                await module.prepare_workspace(self.workspace, 5)
        await module.prepare_workspace(self.workspace, 5)
        assert json.loads(manifest.read_text()) == module.NODE_PACKAGE

    async def test_environment_total_timeout_releases_lock(self) -> None:

        async def slow(command, *args, **kwargs):
            if command[:2] == ['pnpm', 'install']:
                await asyncio.Event().wait()
            return await self.run_command(command, *args, **kwargs)
        with patch.object(module, '_run', side_effect=slow), patch.object(module, 'ENVIRONMENT_TIMEOUT', 0.05):
            with pytest.raises(TimeoutError):
                await module.prepare_workspace(self.workspace, 5)
        assert json.loads((self.workspace / '.office-env.json').read_text())['status'] != 'ready'
        await asyncio.wait_for(module.prepare_workspace(self.workspace, 5), 2)

    async def test_missing_node_dependency_is_restored(self) -> None:
        await module.prepare_workspace(self.workspace, 5)
        (self.workspace / 'node_modules/docx').rmdir()
        self.calls.clear()
        await module.prepare_workspace(self.workspace, 5)
        assert (self.workspace / 'node_modules/docx').exists()
        assert any((args[:2] == ['pnpm', 'install'] for args, _, _ in self.calls))

    async def test_same_user_serializes_but_other_user_can_proceed(self) -> None:
        started, release = (asyncio.Event(), asyncio.Event())

        async def slow(command, workspace, timeout, **kwargs):
            if workspace == self.workspace and command[:2] == ['uv', 'venv']:
                started.set()
                await release.wait()
            return await self.run_command(command, workspace, timeout, **kwargs)
        with patch.object(module, '_run', side_effect=slow):
            first = asyncio.create_task(module.prepare_workspace(self.workspace, 5))
            await asyncio.wait_for(started.wait(), 2)
            second = asyncio.create_task(module.prepare_workspace(self.workspace, 5))
            try:
                await asyncio.wait_for(module.prepare_workspace(self.root / 'user-b', 5), 2)
                assert not second.done()
            finally:
                release.set()
                await asyncio.gather(first, second)
        installs = [args for args, workspace, _ in self.calls if workspace == self.workspace and args[:2] == ['uv', 'venv']]
        assert len(installs) == 1

    async def test_cancellation_does_not_mark_ready_and_releases_lock(self) -> None:
        started = asyncio.Event()

        async def slow(command, *args, **kwargs):
            if command[:2] == ['pnpm', 'install']:
                started.set()
                await asyncio.Event().wait()
            return await self.run_command(command, *args, **kwargs)
        with patch.object(module, '_run', side_effect=slow):
            task = asyncio.create_task(module.prepare_workspace(self.workspace, 5))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert json.loads((self.workspace / '.office-env.json').read_text())['status'] == 'installing'
        await asyncio.wait_for(module.prepare_workspace(self.workspace, 5), 2)

    def test_environment_paths_are_per_user_without_global_mutation(self) -> None:
        before = dict(os.environ)
        first = module.workspace_environment(self.workspace)
        second = module.workspace_environment(self.root / 'user-b')
        assert first['VIRTUAL_ENV'] != second['VIRTUAL_ENV']
        assert first['PATH'].startswith(str(self.workspace / '.venv/bin'))
        assert str(self.workspace) not in second['PATH']
        assert dict(os.environ) == before
