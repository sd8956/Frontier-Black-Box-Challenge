#!/usr/bin/env python3
"""Runs every suite with one command and prints a summary.

    python3 tests/run_all.py

Standard library only: no third-party runner, no configuration, no network.
Each suite is also runnable on its own, which is what the two entries below
spell out. This exists because the delivery asks for one command and its
output, and because "which tests do we have" should not require reading the
tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name -> the file that owns it. The names match the two components: they
# never import each other, and neither does this runner. The offline registry
# and adapter suites need no database; the live suites are opt-in: without
# DATABASE_URL or DB_URL they print one SKIP line and pass.
SUITES = (
    ("api", ROOT / "tests" / "api" / "test_examples.py"),
    ("v2", ROOT / "tests" / "api" / "test_v2.py"),
    ("adapter", ROOT / "tests" / "api" / "test_adapter.py"),
    ("ledger", ROOT / "tests" / "api" / "test_ledger.py"),
    ("registry", ROOT / "tests" / "api" / "test_registry.py"),
    ("ledger-live", ROOT / "tests" / "api" / "test_ledger_live.py"),
    ("registry-live", ROOT / "tests" / "api" / "test_registry_live.py"),
    ("adapter-live", ROOT / "tests" / "api" / "test_adapter_live.py"),
    ("explorer", ROOT / "tests" / "explorer" / "test_control.py"),
)


def run_one(name: str, path: Path) -> bool:
    print("=" * 68)
    print("suite %s  ->  %s" % (name, path.relative_to(ROOT)))
    print("=" * 68)
    result = subprocess.run([sys.executable, str(path)], cwd=str(ROOT))
    print()
    return result.returncode == 0


def main() -> int:
    failed = [name for name, path in SUITES if not run_one(name, path)]
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("all suites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
