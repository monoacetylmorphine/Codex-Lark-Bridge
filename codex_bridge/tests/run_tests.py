"""Backward-compatible pytest entry point for the Codex bridge test suite."""

from __future__ import annotations

import argparse

import pytest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    pytest_args = ["tests"]
    if args.pattern != "test*.py":
        pytest_args.extend(["-k", args.pattern.removesuffix(".py")])
    if args.reverse:
        pytest_args.append("--reverse")
    if args.verbose:
        pytest_args.append("-v")
    return pytest.main(pytest_args)


if __name__ == "__main__":
    raise SystemExit(main())
