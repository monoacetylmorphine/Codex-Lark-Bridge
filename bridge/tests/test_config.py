from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from bridge.configs import loader as config_loader
from bridge.configs.loader import CONFIG, _load_config


class ConfigTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(value))
            return _load_config(path)

    def test_current_config(self) -> None:
        self.assertEqual(self.load(CONFIG), CONFIG)

    def test_session_history_limit_default_and_validation(self) -> None:
        self.assertEqual(CONFIG['messaging']['sessionHistoryLimit'], 30)
        config = copy.deepcopy(CONFIG)
        for value in (1, 30, 60):
            config['messaging']['sessionHistoryLimit'] = value
            self.assertEqual(self.load(config)['messaging']['sessionHistoryLimit'], value)
        for value in (0, -1, True, '30', 1.5, None):
            with self.subTest(value=value):
                config['messaging']['sessionHistoryLimit'] = value
                with self.assertRaisesRegex(RuntimeError, 'messaging.sessionHistoryLimit'):
                    self.load(config)
        config['messaging'].pop('sessionHistoryLimit')
        self.load(config)  # Support legacy runtime configuration; consumers apply defaults.

    def test_shared_mcp_section_is_required_and_validated(self) -> None:
        config = copy.deepcopy(CONFIG)
        config.pop('mcp')
        with self.assertRaises(RuntimeError):
            self.load(config)
        config['mcp'] = {'port': 'invalid', 'custom': True}
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_bot_identity_requires_at_least_one_environment_field(self) -> None:
        for fields in (("robotUnionIdEnv",), ("robotNameEnv",),
                       ("robotUnionIdEnv", "robotNameEnv"), ()):
            with self.subTest(fields=fields):
                config = copy.deepcopy(CONFIG)
                credentials = config["lark"]["credentials"]
                for key in ("robotUnionIdEnv", "robotNameEnv"):
                    if key not in fields:
                        credentials.pop(key, None)
                if fields:
                    self.load(config)
                else:
                    with self.assertRaisesRegex(RuntimeError, "lark.credentials"):
                        self.load(config)

        config = copy.deepcopy(CONFIG)
        credentials = config["lark"]["credentials"]
        credentials["robotOpenIdEnv"] = credentials.pop("robotUnionIdEnv")
        with self.assertRaisesRegex(RuntimeError, "lark.credentials"):
            self.load(config)

    def test_full_schema_and_finite_numbers(self) -> None:
        cases = [('messaging', 'historyPageSize', '10'), ('audio', 'limits', {}),
                 ('messaging', 'historyPageSize', 70000), ('runtime', 'timezoneOffsetHours', float('nan'))]
        for section, key, value in cases:
            with self.subTest(key=key):
                config = copy.deepcopy(CONFIG)
                config[section][key] = value
                with self.assertRaises(RuntimeError):
                    self.load(config)
        config = copy.deepcopy(CONFIG)
        del config['messages']['audioConversionTimeout']
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_route_partition_and_distinct_commands(self) -> None:
        for key, value in [('directTypes', ['text', 'image']), ('directTypes', ['text']),
                           ('newThreadCommand', '/STOP')]:
            config = copy.deepcopy(CONFIG)
            config['messaging'][key] = value
            with self.assertRaises(RuntimeError):
                self.load(config)

    def test_errors_do_not_echo_sensitive_values(self) -> None:
        config = copy.deepcopy(CONFIG)
        config['messaging']['historyPageSize'] = 'private-secret-value'
        with self.assertRaises(RuntimeError) as error:
            self.load(config)
        self.assertIn('messaging.historyPageSize', str(error.exception))
        self.assertNotIn('private-secret-value', str(error.exception))

    def test_missing_and_malformed_config_report_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            with self.assertRaisesRegex(RuntimeError, 'Configuration file does not exist') as caught:
                _load_config(path)
            self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
            path.write_text('{invalid', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'Invalid config.json format') as caught:
                _load_config(path)
            self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)

    def test_missing_malformed_and_invalid_schema_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'schema.json'
            with patch.object(config_loader, 'SCHEMA_FILE', path):
                for content in (None, '{invalid', '{"type": "not-a-type"}'):
                    with self.subTest(content=content):
                        if content is not None:
                            path.write_text(content, encoding='utf-8')
                        with self.assertRaisesRegex(RuntimeError, 'Unable to load configuration Schema') as caught:
                            self.load(CONFIG)
                        self.assertIsNotNone(caught.exception.__cause__)

    def test_unknown_keys_and_missing_required_sections_are_rejected(self) -> None:
        for section in ('storage', 'messaging', 'audio', 'codex', 'lark'):
            for operation in ('missing', 'unknown'):
                with self.subTest(section=section, operation=operation):
                    config = copy.deepcopy(CONFIG)
                    if operation == 'missing':
                        del config[section]
                    else:
                        config[section]['typo'] = 'value'
                    with self.assertRaises(RuntimeError):
                        self.load(config)

    def test_boolean_numbers_and_out_of_range_values_are_rejected(self) -> None:
        for section, key, value in [
            ('messaging', 'historyPageSize', True), ('messaging', 'historyPageSize', 0),
            ('messaging', 'historyPageSize', 0),
            ('runtime', 'timezoneOffsetHours', float('inf')),
            ('runtime', 'timezoneOffsetHours', float('-inf')),
        ]:
            with self.subTest(section=section, key=key, value=value):
                config = copy.deepcopy(CONFIG)
                config[section][key] = value
                with self.assertRaises(RuntimeError):
                    self.load(config)

    def test_commands_with_whitespace_are_rejected(self) -> None:
        for key in ('newThreadCommand', 'stopThreadCommand'):
            for value in (' /command', '/command ', '/com mand', '/command\n', '/com\tmand'):
                with self.subTest(key=key, value=value):
                    config = copy.deepcopy(CONFIG)
                    config['messaging'][key] = value
                    with self.assertRaises(RuntimeError):
                        self.load(config)

    def test_reliability_settings_are_optional_and_validated(self) -> None:
        config = copy.deepcopy(CONFIG)
        for parent, key in ((config['messaging'], 'maxPendingEvents'),
                            (config['lark'], 'requestConcurrency'),
                            (config['logging'], 'logLevel'),
                            (config['codex']['watchdog'], 'finalizationTimeoutSeconds'),
                            (config['messages'], 'cleanupTimeout')):
            parent.pop(key, None)
        self.load(config)
        for keys, value in ((('messaging', 'maxPendingEvents'), 0),
                            (('lark', 'requestConcurrency'), 0),
                            (('logging', 'logLevel'), 'INVALID'),
                            (('codex', 'watchdog', 'finalizationTimeoutSeconds'), 0)):
            invalid = copy.deepcopy(CONFIG)
            parent = invalid
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = value
            with self.assertRaises(RuntimeError):
                self.load(invalid)

    def test_extension_env_name_url_and_sql_identifier_constraints(self) -> None:
        cases = [(('resources', 'acceptedExtensions', 'image'), ['PNG']),
                 (('resources', 'acceptedExtensions', 'image'), ['.png', '.png']),
                 (('audio', 'asr', 'baseUrl'), 'http://example.com'),
                 (('audio', 'asr', 'apiKeyEnv'), 'invalid-key'),
                 (('logging', 'tokenUsageTable'), 'usage; DROP TABLE users')]
        for keys, value in cases:
            with self.subTest(keys=keys, value=value):
                config = copy.deepcopy(CONFIG)
                parent = config
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.assertRaises(RuntimeError):
                    self.load(config)

    def test_loading_does_not_mutate_source_file_or_global_config(self) -> None:
        before = copy.deepcopy(CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            value = copy.deepcopy(CONFIG)
            value['storage']['databasePath'] = 'state.sqlite'
            raw = json.dumps(value)
            path.write_text(raw, encoding='utf-8')
            loaded = _load_config(path)
            self.assertEqual(loaded['storage']['databasePath'], str(path.parent.resolve() / 'state.sqlite'))
            self.assertEqual(path.read_text(encoding='utf-8'), raw)
        self.assertEqual(CONFIG, before)

    def test_deprecated_fields_are_rejected(self) -> None:
        for keys, value in [(('storage', 'logdatabasePath'), 'unused.sqlite'),
                            (('codex', 'models', 'file'), {'model': 'unused', 'provider': 'unused'})]:
            config = copy.deepcopy(CONFIG)
            parent = config
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = value
            with self.assertRaises(RuntimeError):
                self.load(config)

    def test_shared_upload_is_validated_without_initializing_mcp(self) -> None:
        config = copy.deepcopy(CONFIG)
        config['lark'].pop('upload')
        with self.assertRaises(RuntimeError):
            self.load(config)
        config['lark']['upload'] = {'fallbackFileType': None}
        with self.assertRaises(RuntimeError):
            self.load(config)
