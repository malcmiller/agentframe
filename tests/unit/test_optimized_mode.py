"""Internal invariant checks still run under ``python -O``.

``python -O`` removes ``assert`` statements, so the plan executor and the A2A
executor check their invariants with ``if ...: raise RuntimeError``. This
runs their invariant tests again in an optimized interpreter. The tests use
``pytest.raises``, which ``-O`` keeps, so a check written as ``assert``
would fail there.
"""

import re
import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).parents[2]
PLAN_EXECUTION = "tests/unit/orchestration/test_plan_execution_invariants.py"
A2A_EXECUTOR = "tests/unit/hosting/test_a2a_executor.py"
INVARIANT_TESTS = [
    f"{PLAN_EXECUTION}::test_invariant_step_outside_the_catalog_is_refused_before_counting",
    f"{PLAN_EXECUTION}::test_invariant_plan_checks_need_a_running_skill",
    f"{PLAN_EXECUTION}::test_invariant_tool_checks_need_a_running_skill",
    f"{PLAN_EXECUTION}::test_invariant_token_share_needs_a_token_limit",
    f"{A2A_EXECUTOR}::test_invariant_execute_needs_a_message",
    f"{A2A_EXECUTOR}::test_invariant_cancel_needs_task_and_context_ids",
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
            *INVARIANT_TESTS,
        ],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert re.search(rf"\b{len(INVARIANT_TESTS)} passed\b", finished.stdout)
