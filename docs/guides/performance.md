# Performance baseline

How much AgentFrame adds around a model call, measured with `ScriptedModel`
so no provider is called (#256). A real model's latency is usually
hundreds of milliseconds to seconds, far above the overhead below; the
exception is the A2A hop, whose cost comes from how `Remote` waits for the
peer's task.

## Recorded baseline

Recorded on 2026-11-15 from `main` plus this benchmark, on one developer
machine: AMD Ryzen 9 5950X, Windows 11 Pro, CPython 3.13.0, `a2a-sdk` 1.3.0,
Uvicorn 0.54.0. Three consecutive runs; the ranges show all three.

| Benchmark | Result | What it measures |
| --- | --- | --- |
| Local run overhead | 0.05 ms (run p95 0.10-0.11 ms) | Median `Runtime.run` of a one-model-call skill minus the median bare `ScriptedModel.generate`: validation, limits, accounting and context. |
| Hosted run latency | 4.8-5.8 ms (p95 7.1-7.4 ms) | Median blocking skill call from the official A2A SDK client to an `AgentApp` over localhost HTTP, end to end. |
| A2A hop overhead | 266-274 ms (hop p95 269-280 ms) | Median local orchestrator run making one `Remote.call` to that hosted agent, minus a median local run. |
| Throughput at the admission limit | 138-181 runs/s, 0 refused | 800 calls from 8 concurrent callers to a host with `max_concurrent_runs=8`; the scripted model takes 20 ms per reply. |
| Throughput at twice the limit | 150-171 completed runs/s, 10-22 of 800 refused | The same with 16 callers; refused calls get HTTP 503 and are not retried. |

What the numbers show:

- **A local run costs about 0.05 ms** beyond the model call.
- **A hosted call costs about 5 ms** on localhost, most of it HTTP, the A2A
  SDK's JSON-RPC handling and the task store.
- **An A2A hop costs about 265 ms, almost all of it waiting.** `Remote.ask`
  and `call` send with `return_immediately` and then poll the task, first
  after 0.25 seconds (then 0.5, then every second; see
  [`Remote.ask`](../api/discovery.md#remoteask)). A peer that finishes in a few
  milliseconds is still seen as finished only at the first poll, so a
  sequential chain of N hops waits at least N × 0.25 seconds. Fan-out with
  `asyncio.TaskGroup` waits for the polls in parallel.
- **Throughput at the limit is bounded by the limit and run time.** With 8
  slots and a 20 ms model the ceiling is 400 runs/s; the client and server
  share one event loop here, so the measured rate also pays for the client.
  Admission takes a slot when the `SendMessage` request arrives and releases
  it when the run ends, so the slot is free while the response goes back and
  while the client, on the same event loop, prepares its next call. Twice the
  callers therefore keep the slots full only part of the time, and only a few
  requests are refused.

### Caveats

- One machine, one process: the server, the client and the benchmark share
  one event loop and one CPU core, so these are a relative baseline for
  catching regressions, not a server's capacity. A deployed host runs its
  clients elsewhere.
- Localhost HTTP, anonymous access (`AllowAnonymous`), the default in-memory
  task store and deduplication. Authentication, a persistent task store or a
  real network add their own cost.
- Medians of 2,000 local runs, 300 hosted calls and 20 hops, after 20
  untimed warm-up calls each. Timings vary from run to run (the ranges above);
  the GitHub-hosted runners that CI uses are slower and noisier than this
  machine.

## Running the benchmarks

The benchmarks are opt-in pytest tests in `tests/benchmarks/`, skipped by the
default `uv run pytest`:

```bash
AGENTFRAME_BENCHMARK=1 uv run pytest tests/benchmarks -m benchmark -s
```

`-s` prints each result. Set `AGENTFRAME_BENCHMARK_REPORT` to a file path to
also append each result to it as a Markdown list item.

## Regression check in CI

The `benchmarks` job in `.github/workflows/ci.yml` runs them on every push
and pull request on Python 3.13 and writes the results to the job summary.
It is non-gating (`continue-on-error: true`): a failure marks the job but
does not block a merge. Each test fails only when its result passes a ceiling
set well above the baseline, to allow for slower, noisier runners:

| Benchmark | Fails when |
| --- | --- |
| Local run overhead | above 1 ms |
| Hosted run latency | above 50 ms |
| A2A hop overhead | above 400 ms |
| Throughput at the admission limit | below 50 runs/s, or any call refused |

When the job fails, compare its summary with the baseline above and rerun it
before investigating; a single slow runner can trip a ceiling. When a change
moves a result on purpose (for example a different polling schedule), record
the new baseline here and adjust the ceilings in
`tests/benchmarks/test_overhead.py`.
