"""``JSONFormatter``: one JSON object per log record, with run correlation IDs."""

import json
import logging
import sys

from agentframe.log_format import JSONFormatter
from agentframe.runtime.correlation import RunCorrelation


def record(message: str, **extra: object) -> logging.LogRecord:
    entry = logging.LogRecord(
        "agentframe.hosting", logging.INFO, __file__, 1, message, (), None
    )
    entry.__dict__.update(extra)
    return entry


def test_a_run_record_carries_its_correlation_ids() -> None:
    correlation = RunCorrelation(
        "echo", "inv-2", root_invocation_id="inv-1", parent_invocation_id="inv-1"
    )

    entry = json.loads(
        JSONFormatter().format(record("Run started", **correlation.log_extra()))
    )

    assert entry["level"] == "INFO"
    assert entry["logger"] == "agentframe.hosting"
    assert entry["message"] == "Run started"
    assert entry["time"].endswith("+00:00")
    assert entry["agent"] == "echo"
    assert entry["invocation_id"] == "inv-2"
    assert entry["root_invocation_id"] == "inv-1"
    assert entry["parent_invocation_id"] == "inv-1"


def test_a_root_run_has_a_null_parent() -> None:
    correlation = RunCorrelation("echo", "inv-1", root_invocation_id="inv-1")

    entry = json.loads(
        JSONFormatter().format(record("Run started", **correlation.log_extra()))
    )

    assert entry["parent_invocation_id"] is None


def test_other_records_hold_only_their_message_and_never_other_extras() -> None:
    entry = json.loads(JSONFormatter().format(record("Ready", api_key="secret")))

    assert set(entry) == {"time", "level", "logger", "message"}


def test_a_traceback_is_one_json_field_on_one_line() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        failure = record("The skill failed.")
        failure.exc_info = sys.exc_info()

    line = JSONFormatter().format(failure)

    assert "\n" not in line
    assert "ValueError: boom" in json.loads(line)["exception"]
