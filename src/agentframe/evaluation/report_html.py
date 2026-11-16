"""Render an evaluation's JSON report document as one self-contained HTML page.

The page is built from the document ``report_json`` returns, so it never
disagrees with the JSON report: it holds content (case inputs, outputs, score
reasons, error messages) only when that document does. It uses inline CSS and
one small inline script that sorts and filters the case table, with no
external URLs. A Content-Security-Policy blocks every fetch and any script but
that one. Every value from the report is HTML-escaped, because case IDs,
outputs and judge reasons are untrusted.
"""

import base64
import hashlib
import json
from collections.abc import Mapping
from html import escape
from pathlib import Path
from typing import Any, TypeGuard

from agentframe.evaluation.report_format import SCHEMA, percent

_LATENCY_KEYS = ("p50_seconds", "p95_seconds", "max_seconds")

_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1f2328; }
h1 { font-size: 1.5rem; }
h2 { font-size: 1.2rem; margin-top: 2rem; }
table { border-collapse: collapse; margin: 0.5rem 0; }
th, td { border: 1px solid #d0d7de; padding: 0.3rem 0.6rem; text-align: left;
  vertical-align: top; }
th { background: #f6f8fa; }
th button { all: unset; cursor: pointer; font-weight: bold; }
pre { background: #f6f8fa; padding: 0.5rem; white-space: pre-wrap;
  overflow-wrap: anywhere; }
details { margin: 0.5rem 0; border: 1px solid #d0d7de; padding: 0.5rem; }
summary { cursor: pointer; font-weight: bold; }
.passed { color: #1a7f37; }
.failed, .errored, .newly-failing { color: #cf222e; }
.not_run { color: #9a6700; }
.note { color: #59636e; }
"""

# Sorts the case table by a clicked column and filters it by text and status.
_SCRIPT = """
const table = document.getElementById("cases");
const body = table.tBodies[0];
const rows = Array.from(body.rows);
const textFilter = document.getElementById("case-filter");
const statusFilter = document.getElementById("status-filter");

function applyFilters() {
  const text = textFilter.value.toLowerCase();
  const status = statusFilter.value;
  for (const row of rows) {
    const textMatches = row.textContent.toLowerCase().includes(text);
    const statusMatches = status === "" || row.dataset.status === status;
    row.hidden = !(textMatches && statusMatches);
  }
}

function sortValue(row, column) {
  const cell = row.cells[column];
  return cell.dataset.sort ?? cell.textContent;
}

function compare(a, b) {
  const numbers = a !== "" && b !== "" && !isNaN(a) && !isNaN(b);
  return numbers ? Number(a) - Number(b) : a.localeCompare(b);
}

for (const header of table.tHead.rows[0].cells) {
  header.querySelector("button").addEventListener("click", () => {
    const ascending = header.getAttribute("aria-sort") !== "ascending";
    for (const other of table.tHead.rows[0].cells) {
      other.removeAttribute("aria-sort");
    }
    header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    const direction = ascending ? 1 : -1;
    const column = header.cellIndex;
    rows.sort((a, b) => {
      return direction * compare(sortValue(a, column), sortValue(b, column));
    });
    body.append(...rows);
  });
}

textFilter.addEventListener("input", applyFilters);
statusFilter.addEventListener("change", applyFilters);
"""

# Errored, failed and not-run attempts come before passed ones, as in the
# console summary.
_STATUS_ORDER = {"errored": 0, "failed": 1, "not_run": 2, "passed": 3}


def read_baseline(path: str | Path) -> dict[str, Any]:
    """Read an earlier JSON report to compare a new one against.

    Args:
        path: A file ``EvalReport.write_json`` wrote.

    Returns:
        The report document.

    Raises:
        ValueError: The file is not an ``agentframe.evaluation.report/1``
            JSON report.
        OSError: The file cannot be read.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    # Deeply nested JSON can make the parser raise RecursionError, not ValueError.
    except (ValueError, RecursionError) as error:
        raise ValueError(f"The baseline '{path}' is not a JSON file.") from error
    if not _is_report(document):
        raise ValueError(
            f"The baseline '{path}' is not an AgentFrame evaluation report ({SCHEMA})."
        )
    return document


def report_html(document: dict[str, Any], baseline: dict[str, Any] | None) -> str:
    """Render a JSON report document, and an optional baseline, as an HTML page."""
    results = document["results"]
    body = [
        _header(document),
        _summary(document),
        _rates(
            "Scorer pass rates", "Scorer", document["aggregates"]["scorer_pass_rates"]
        ),
        _rates("Tag pass rates", "Tag", document["aggregates"]["tag_pass_rates"]),
        _counts(
            "Errors by class", "Error class", document["aggregates"]["errors_by_class"]
        ),
        _baseline(document, baseline) if baseline is not None else "",
        _case_table(results),
        _case_details(results, capture_content=document["capture_content"]),
    ]
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_policy()}">\n'
        f"<title>AgentFrame evaluation: {_text(_target(document))}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n"
        + "\n".join(part for part in body if part)
        + f"\n<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )


def _policy() -> str:
    # Only the page's own style and script may run; nothing may be fetched.
    return (
        "default-src 'none'; "
        f"style-src '{_sha256(_STYLE)}'; script-src '{_sha256(_SCRIPT)}'; "
        "base-uri 'none'; form-action 'none'"
    )


def _sha256(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def _text(value: object) -> str:
    """Any value as HTML-escaped text, safe in element content and attributes."""
    return escape(str(value), quote=True)


def _header(document: dict[str, Any]) -> str:
    budget = document["max_total_tokens"]
    content_note = (
        "Content was captured: treat this file as sensitive."
        if document["capture_content"]
        else "Content was not captured: no case inputs, outputs, reasons or "
        "error messages."
    )
    return (
        "<h1>AgentFrame evaluation report</h1>\n"
        "<table>\n"
        + _row("Target", _target(document))
        + _row("Scorers", ", ".join(document["scorers"]) or "-")
        + _row("Budget", f"{budget} tokens" if budget is not None else "none")
        + _row("AgentFrame", document["agentframe_version"])
        + "</table>\n"
        f'<p class="note">{_text(content_note)}</p>'
    )


def _target(document: dict[str, Any]) -> str:
    target = document["target"]
    return ", ".join(f"{key}={value}" for key, value in target.items()) or "-"


def _summary(document: dict[str, Any]) -> str:
    aggregates = document["aggregates"]
    judge_usage = aggregates["total_judge_usage"]
    rows = [
        _row("Attempts", aggregates["attempt_count"]),
        _row("Passed", aggregates["passed_count"]),
        _row("Failed", aggregates["failed_count"]),
        _row("Errored", aggregates["errored_count"]),
        _row("Not run", aggregates["not_run_count"]),
        _row("Pass rate", percent(aggregates["pass_rate"])),
        _row("Tokens", _usage_totals(aggregates["total_usage"])),
    ]
    if judge_usage is not None:
        rows.append(_row("Judge tokens", _usage_totals(judge_usage)))
    if document["max_total_tokens"] is not None:
        rows.append(_row("Over budget", "yes" if aggregates["over_budget"] else "no"))
        enforced = aggregates["budget_fully_enforced"]
        rows.append(_row("Budget fully enforced", "yes" if enforced else "no"))
    rows.append(_row("Latency", _latency(aggregates["latency"])))
    rows.append(_row("Flaky cases", ", ".join(aggregates["flaky_cases"]) or "none"))
    return "<h2>Summary</h2>\n<table>\n" + "".join(rows) + "</table>"


def _row(label: str, value: object) -> str:
    return f"<tr><th>{_text(label)}</th><td>{_text(value)}</td></tr>\n"


def _usage_totals(usage: dict[str, Any]) -> str:
    if usage["total_tokens"] is None:
        return (
            f"unknown; {usage['known_total_tokens']} reported, "
            f"{usage['unknown_cases']} cases with unknown usage"
        )
    estimated = " (estimated)" if usage["estimated"] else ""
    return f"{usage['total_tokens']} total{estimated}"


def _latency(latency: dict[str, float] | None) -> str:
    if latency is None:
        return "-"
    return (
        f"p50 {_seconds(latency['p50_seconds'])}, "
        f"p95 {_seconds(latency['p95_seconds'])}, "
        f"max {_seconds(latency['max_seconds'])}"
    )


def _seconds(seconds: float | None) -> str:
    return f"{seconds:.2f}s" if seconds is not None else "-"


def _rates(title: str, label: str, rates: dict[str, float]) -> str:
    return _named_values(
        title, label, "Pass rate", {name: percent(rate) for name, rate in rates.items()}
    )


def _counts(title: str, label: str, counts: dict[str, int]) -> str:
    return _named_values(title, label, "Attempts", counts)


def _named_values(
    title: str, label: str, heading: str, values: Mapping[str, object]
) -> str:
    if not values:
        return ""
    rows = "".join(
        f"<tr><td>{_text(name)}</td><td>{_text(value)}</td></tr>\n"
        for name, value in values.items()
    )
    return (
        f"<h2>{_text(title)}</h2>\n<table>\n"
        f"<tr><th>{_text(label)}</th><th>{_text(heading)}</th></tr>\n"
        f"{rows}</table>"
    )


def _baseline(document: dict[str, Any], baseline: dict[str, Any]) -> str:
    current = _case_outcomes(document["results"])
    previous = _case_outcomes(baseline["results"])
    newly_failing = [
        case_id
        for case_id, passed in current.items()
        if not passed and previous.get(case_id) is True
    ]
    newly_passing = [
        case_id
        for case_id, passed in current.items()
        if passed and previous.get(case_id) is False
    ]
    rate = document["aggregates"]["pass_rate"]
    baseline_rate = baseline["aggregates"]["pass_rate"]
    return (
        "<h2>Compared with the baseline</h2>\n"
        '<p class="note">A case passes when every attempt of it that ran passed. '
        f"Baseline target: {_text(_target(baseline))}.</p>\n"
        "<table>\n"
        "<tr><th></th><th>Baseline</th><th>This run</th><th>Change</th></tr>\n"
        + _delta_row(
            "Pass rate",
            percent(baseline_rate),
            percent(rate),
            f"{(rate - baseline_rate) * 100:+.1f} points",
        )
        + _latency_rows(
            baseline["aggregates"]["latency"], document["aggregates"]["latency"]
        )
        + "</table>\n"
        + _case_list("Newly failing", newly_failing, "newly-failing")
        + _case_list("Newly passing", newly_passing, "passed")
    )


def _case_outcomes(results: list[dict[str, Any]]) -> dict[str, bool]:
    """Whether each case passed, by case ID; cases that never ran are left out."""
    statuses: dict[str, set[str]] = {}
    for result in results:
        if result["status"] != "not_run":
            statuses.setdefault(result["case_id"], set()).add(result["status"])
    return {case_id: seen == {"passed"} for case_id, seen in statuses.items()}


def _delta_row(label: str, before: str, after: str, change: str) -> str:
    cells = "".join(f"<td>{_text(cell)}</td>" for cell in (before, after, change))
    return f"<tr><th>{_text(label)}</th>{cells}</tr>\n"


def _latency_rows(
    before: dict[str, float] | None, after: dict[str, float] | None
) -> str:
    rows = []
    for key in _LATENCY_KEYS:
        old = before[key] if before is not None else None
        new = after[key] if after is not None else None
        change = f"{new - old:+.2f}s" if old is not None and new is not None else "-"
        label = "Latency " + key.removesuffix("_seconds")
        rows.append(_delta_row(label, _seconds(old), _seconds(new), change))
    return "".join(rows)


def _case_list(title: str, case_ids: list[str], css_class: str) -> str:
    if not case_ids:
        return f"<h3>{_text(title)}</h3>\n<p>None.</p>"
    items = "".join(
        f'<li class="{css_class}">{_text(case_id)}</li>' for case_id in case_ids
    )
    return f"<h3>{_text(title)}</h3>\n<ul>{items}</ul>"


def _case_table(results: list[dict[str, Any]]) -> str:
    columns = ("Case", "Attempt", "Status", "Tags", "Scores", "Tokens", "Duration")
    headers = "".join(
        f'<th><button type="button">{_text(column)}</button></th>' for column in columns
    )
    statuses = "".join(
        f'<option value="{status}">{status}</option>' for status in _STATUS_ORDER
    )
    ordered = sorted(
        enumerate(results), key=lambda item: _STATUS_ORDER.get(item[1]["status"], 0)
    )
    rows = "".join(_case_row(index, result) for index, result in ordered)
    return (
        "<h2>Cases</h2>\n"
        '<p><label>Filter <input type="search" id="case-filter"></label> '
        '<label>Status <select id="status-filter"><option value="">all</option>'
        f"{statuses}</select></label></p>\n"
        f'<table id="cases">\n<thead><tr>{headers}</tr></thead>\n'
        f"<tbody>\n{rows}</tbody>\n</table>"
    )


def _case_row(index: int, result: dict[str, Any]) -> str:
    status = result["status"]
    duration = result["duration_seconds"]
    tokens = _attempt_tokens(result)
    usage = result["usage"]
    known = usage is not None and usage["total_tokens"] is not None
    tokens_sort = usage["total_tokens"] if known else ""
    cells = [
        f'<td><a href="#attempt-{index}">{_text(result["case_id"])}</a></td>',
        f"<td>{_text(result['attempt'])}</td>",
        f'<td class="{_text(status)}">{_text(status)}</td>',
        f"<td>{_text(', '.join(result['tags']))}</td>",
        f"<td>{_text(_scores_cell(result))}</td>",
        f'<td data-sort="{_text(tokens_sort)}">{_text(tokens)}</td>',
        f'<td data-sort="{_text(duration if duration is not None else "")}">'
        f"{_text(_seconds(duration))}</td>",
    ]
    return f'<tr data-status="{_text(status)}">{"".join(cells)}</tr>\n'


def _attempt_tokens(result: dict[str, Any]) -> str:
    if result["status"] == "not_run":
        return "-"
    return _tokens(result["usage"])


def _tokens(usage: dict[str, Any] | None) -> str:
    """One run's total tokens: ``~`` marks an estimate, unknown is never 0."""
    if usage is None or usage["total_tokens"] is None:
        return "unknown"
    total = str(usage["total_tokens"])
    return f"~{total}" if usage["estimated"] else total


def _scores_cell(result: dict[str, Any]) -> str:
    cells = [
        f"{score['scorer']}={'pass' if score['passed'] else 'fail'}"
        for score in result["scores"]
    ]
    if result["error_type"] is not None:
        cells.append(f"error={result['error_type']}")
    return " ".join(cells) or "-"


def _case_details(results: list[dict[str, Any]], *, capture_content: bool) -> str:
    details = "\n".join(
        _attempt_details(index, result, capture_content=capture_content)
        for index, result in enumerate(results)
    )
    return f"<h2>Case details</h2>\n{details}"


def _attempt_details(
    index: int, result: dict[str, Any], *, capture_content: bool
) -> str:
    status = result["status"]
    is_open = " open" if status != "passed" else ""
    facts = [
        _row("Status", status),
        _row("Tags", ", ".join(result["tags"]) or "-"),
        _row("Invocation ID", result["invocation_id"] or "-"),
        _row("Error type", result["error_type"] or "-"),
    ]
    if result["error_message"] is not None:
        facts.append(_row("Error message", result["error_message"]))
    judge_usage = result["judge_usage"]
    facts += [
        _row("Tokens", _attempt_tokens(result)),
        _row("Judge tokens", _tokens(judge_usage) if judge_usage else "-"),
        _row("Duration", _seconds(result["duration_seconds"])),
        _row("Model calls", _or_dash(result["model_calls"])),
        _row("Tool calls", _or_dash(result["tool_calls"])),
        _row("Finish reason", _or_dash(result["finish_reason"])),
    ]
    return (
        f'<details id="attempt-{index}"{is_open}>\n'
        f"<summary>{_text(result['case_id'])} #{_text(result['attempt'])}: "
        f'<span class="{_text(status)}">{_text(status)}</span></summary>\n'
        f"<table>\n{''.join(facts)}</table>\n"
        + _scores_table(result["scores"], capture_content=capture_content)
        + (_content(result) if capture_content else "")
        + "</details>"
    )


def _or_dash(value: object) -> object:
    return "-" if value is None else value


def _scores_table(scores: list[dict[str, Any]], *, capture_content: bool) -> str:
    if not scores:
        return ""
    headings = ["Scorer", "Result", "Value", "Votes"]
    if capture_content:
        headings.append("Reason")
    rows = []
    for score in scores:
        votes = (
            f"{score['votes_passed']}/{score['votes_cast']} passed"
            if score["votes_cast"] is not None
            else "-"
        )
        value = f"{score['value']:g}" if score["value"] is not None else "-"
        cells = [score["scorer"], "pass" if score["passed"] else "fail", value, votes]
        if capture_content:
            cells.append(_or_dash(score.get("reason")))
        rows.append(
            "<tr>" + "".join(f"<td>{_text(cell)}</td>" for cell in cells) + "</tr>\n"
        )
    header = "".join(f"<th>{heading}</th>" for heading in headings)
    return f"<table>\n<tr>{header}</tr>\n{''.join(rows)}</table>\n"


def _content(result: dict[str, Any]) -> str:
    """The captured case input, expected value and output, as escaped JSON."""
    parts = []
    for key, label in (
        ("arguments", "Arguments"),
        ("text", "Text"),
        ("expected", "Expected"),
        ("output", "Output"),
    ):
        value = result.get(key)
        if value is None:
            continue
        shown = (
            value
            if isinstance(value, str)
            else json.dumps(value, indent=2, ensure_ascii=False)
        )
        parts.append(f"<h4>{label}</h4>\n<pre>{_text(shown)}</pre>\n")
    return "".join(parts)


def _is_report(document: object) -> TypeGuard[dict[str, Any]]:
    """Whether ``document`` has the parts of a version-1 report the diff reads."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return False
    aggregates = document.get("aggregates")
    results = document.get("results")
    if not isinstance(aggregates, dict) or not isinstance(results, list):
        return False
    latency = aggregates.get("latency")
    latency_valid = latency is None or (
        isinstance(latency, dict)
        and all(_is_number(latency.get(key)) for key in _LATENCY_KEYS)
    )
    results_valid = all(
        isinstance(result, dict)
        and isinstance(result.get("case_id"), str)
        and isinstance(result.get("status"), str)
        for result in results
    )
    target_valid = isinstance(document.get("target"), dict)
    return (
        _is_number(aggregates.get("pass_rate"))
        and latency_valid
        and results_valid
        and target_valid
    )


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
