"""Regression coverage for package relocation and mounted storage paths."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from bridge import Codex, LiveTurn
from bridge.codex import codex_execution as codex
from bridge.configs.loader import CONFIG, _load_config


class PackagePathTests(unittest.TestCase):
    def test_public_exports_preserve_class_identity(self) -> None:
        self.assertIs(Codex, codex.Codex)
        self.assertIs(LiveTurn, codex.LiveTurn)

    def test_string_config_path_and_relative_storage(self) -> None:
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            paths = {"runLogPath": "logs", "databasePath": "state.sqlite",
                     "workspaceRoot": "workspace"}
            config["storage"].update(paths)
            file_path = Path(directory) / "config.json"
            file_path.write_text(json.dumps(config), encoding="utf-8")
            loaded = _load_config(str(file_path))
            for key, relative in paths.items():
                self.assertEqual(Path(loaded["storage"][key]), file_path.parent.resolve() / relative)

    def test_old_csv_setting_is_accepted_but_no_default_is_added(self) -> None:
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            config['storage'].pop('tokenUsagePath', None)
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            self.assertNotIn('tokenUsagePath', _load_config(path)['storage'])
            config['storage']['tokenUsagePath'] = 'unused.csv'
            path.write_text(json.dumps(config))
            self.assertEqual(_load_config(path)['storage']['tokenUsagePath'], 'unused.csv')

    def test_storage_paths_expand_current_home(self) -> None:
        with TemporaryDirectory() as directory:
            config = copy.deepcopy(CONFIG)
            paths = {"runLogPath": "~/.codex_bridge/logs", "databasePath": "~/.codex_bridge/state.sqlite",
                     "workspaceRoot": "~/.codex/workspace"}
            config["storage"].update(paths)
            file_path = Path(directory) / "config.json"
            file_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict("os.environ", HOME=str(Path(directory) / "another-user")):
                loaded = _load_config(file_path)
                for key, value in paths.items():
                    self.assertEqual(loaded["storage"][key], str(Path.home() / value[2:]))
