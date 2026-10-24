"""Shared configuration source, atomic first initialization, and protection of existing configuration."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
ROOT = Path(__file__).resolve().parents[2]

class TestSharedConfig:

    def test_both_projects_reference_one_canonical_source(self) -> None:
        for name in ('config_default.json', 'config_schema.json', 'validation.py', 'initialization.py'):
            paths = [(ROOT / project / 'configs' / name).resolve() for project in ('codex_bridge', 'mcp_service')]
            assert paths == [ROOT / 'codex_bridge/configs' / name] * 2

    def test_parallel_initialization_and_existing_config_protection(self) -> None:
        spec = importlib.util.spec_from_file_location('initialize_config_test', ROOT / 'codex_bridge/configs/initialization.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = ROOT / 'codex_bridge/configs/config_default.json'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.codex_bridge/config.json'
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: module.initialize(source, target), range(16)))
            assert json.loads(target.read_text()) == json.loads(source.read_text())
            assert target.stat().st_mode & 511 == 384
            target.write_text('{"existing":true}')
            module.initialize(source, target)
            assert target.read_text() == '{"existing":true}'
            assert list(target.parent.glob('.config-*')) == []
