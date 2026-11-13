"""Tests for reading the correlation values a caller sends: trace context and IDs."""

import pytest

from agentframe.runtime.correlation import (
    TraceContext,
    is_invocation_id,
    read_trace_context,
    root_run,
)

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def test_a_valid_trace_context_is_kept_exactly() -> None:
    trace = read_trace_context([TRACEPARENT], ["congo=t61rcWkgMzE, rojo=00f067aa"])

    assert trace == TraceContext(TRACEPARENT, "congo=t61rcWkgMzE, rojo=00f067aa")
    assert trace.headers() == {
        "traceparent": TRACEPARENT,
        "tracestate": "congo=t61rcWkgMzE, rojo=00f067aa",
    }


def test_several_tracestate_headers_are_joined_as_http_allows() -> None:
    trace = read_trace_context([TRACEPARENT], ["congo=t61", "tenant@vendor=1"])

    assert trace == TraceContext(TRACEPARENT, "congo=t61,tenant@vendor=1")


def test_without_a_tracestate_only_the_traceparent_is_forwarded() -> None:
    trace = read_trace_context([TRACEPARENT], [])

    assert trace is not None
    assert trace.headers() == {"traceparent": TRACEPARENT}


@pytest.mark.parametrize(
    "traceparents",
    [
        pytest.param([], id="missing"),
        pytest.param([TRACEPARENT, TRACEPARENT], id="repeated"),
        pytest.param([TRACEPARENT.upper()], id="uppercase"),
        pytest.param([TRACEPARENT + "\n"], id="trailing-newline"),
        pytest.param([TRACEPARENT + "-extra"], id="extra-field"),
        pytest.param(["01" + TRACEPARENT[2:]], id="later-version"),
        pytest.param(["ff" + TRACEPARENT[2:]], id="version-ff"),
        pytest.param([f"00-{'0' * 32}-b7ad6b7169203331-01"], id="zero-trace-id"),
        pytest.param([f"00-{'1' * 32}-{'0' * 16}-01"], id="zero-parent-id"),
        pytest.param(["not a traceparent"], id="garbage"),
    ],
)
def test_an_invalid_traceparent_drops_the_whole_trace_context(
    traceparents: list[str],
) -> None:
    assert read_trace_context(traceparents, ["congo=t61"]) is None


@pytest.mark.parametrize(
    "tracestate",
    [
        pytest.param("congo=t61\nrojo=1", id="newline"),
        pytest.param("congo=t61\x00", id="control-character"),
        pytest.param("Congo=t61", id="uppercase-key"),
        pytest.param("congo", id="no-value"),
        pytest.param("congo=a=b", id="equals-in-value"),
        pytest.param(f"congo={'x' * 510}", id="over-512-characters"),
        pytest.param(",".join(f"k{n}=v" for n in range(33)), id="over-32-members"),
    ],
)
def test_an_invalid_tracestate_is_dropped_but_the_traceparent_kept(
    tracestate: str,
) -> None:
    assert read_trace_context([TRACEPARENT], [tracestate]) == TraceContext(TRACEPARENT)


def test_an_invocation_id_is_a_lowercase_uuid_string() -> None:
    assert is_invocation_id("6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("6F1C2D3E-4A5B-4C6D-8E7F-901A2B3C4D5E", id="uppercase"),
        pytest.param("{6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e}", id="braces"),
        pytest.param("urn:uuid:6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e", id="urn"),
        pytest.param("6f1c2d3e4a5b4c6d8e7f901a2b3c4d5e", id="no-hyphens"),
        pytest.param("6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e\n", id="newline"),
        pytest.param("run-1", id="not-a-uuid"),
        pytest.param(42, id="number"),
        pytest.param(None, id="null"),
    ],
)
def test_anything_else_is_not_an_invocation_id(value: object) -> None:
    assert not is_invocation_id(value)


def test_a_root_run_is_its_own_root_and_logs_its_ids() -> None:
    correlation = root_run("Planner", "run-1")

    assert correlation.log_extra() == {
        "agent": "Planner",
        "invocation_id": "run-1",
        "root_invocation_id": "run-1",
        "parent_invocation_id": None,
    }
