"""Internal invariant checks still run under ``python -O``.

``python -O`` removes ``assert`` statements, so the plan executor and the A2A
executor check their invariants with ``if ...: raise RuntimeError``. This
runs their invariant tests again in an optimized interpreter. The tests use
``pytest.raises``, which ``-O`` keeps, so a check written as ``assert``
would fail there.
"""

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).parents[2]
INVARIANT_TESTS = [
    "tests/unit/orchestration/test_plan_execution_invariants.py",
    "tests/unit/hosting/test_a2a_executor.py",
]


def test_invariant_checks_run_under_python_o() -> None:
    finished = subprocess.run(
        [
            sys.executable,
            "-O",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-k",
            "invariant",
            *INVARIANT_TESTS,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "6 passed" in finished.stdout
