"""Run offline unittest discovery using a temporary, non-production config.

Usage: python -B tests/run_tests.py [--pattern test_resource_validator.py] [--reverse]
Requires the project's Python >= 3.13 environment and pyproject.toml/uv.lock packages.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def reverse_suite(suite: unittest.TestSuite) -> unittest.TestSuite:
    """Reverse both file/class order and method order to detect leaked state."""
    return unittest.TestSuite(
        reverse_suite(test) if isinstance(test, unittest.TestSuite) else test
        for test in reversed(list(suite))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--reverse', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='codex_bridge-tests-') as directory:
        temporary = Path(directory)
        config = json.loads((root / 'configs/config_default.json').read_text(encoding='utf-8'))
        config['storage'].update(
            databasePath=str(temporary / 'state.sqlite'),
            workspaceRoot=str(temporary / 'workspace'),
        )
        config_path = temporary / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        with patch.dict(os.environ, CONFIG_FILE=str(config_path)), patch('dotenv.load_dotenv'):
            # Load this checkout even when its directory name differs from the package name.
            spec = importlib.util.spec_from_file_location('codex_bridge', root / '__init__.py',
                                                         submodule_search_locations=[str(root)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = package
            spec.loader.exec_module(package)
            suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=args.pattern)
            if args.reverse:
                suite = reverse_suite(suite)
            if suite.countTestCases() == 0:
                parser.error(f'No tests matched {args.pattern!r}')
            result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
