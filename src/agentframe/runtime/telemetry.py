"""Optional OpenTelemetry tracing and metrics for runs.

Tracing is off unless ``Runtime`` or ``AgentApp`` gets a ``TracingConfig``,
and needs the ``otel`` extra (the OpenTelemetry API only). The application
owns the SDK: its tracer and meter providers, exporters and sampling.
AgentFrame never sets global providers.

Spans and metrics carry names, IDs, kinds, counts and error class names only.
Messages, arguments, results, instructions, URLs, credentials, exception
messages and private reasoning are never recorded. Accounting and limits
work the same with tracing on or off.
"""

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from agentframe.errors import DefinitionError
from agentframe.runtime.correlation import TraceContext
from agentframe.usage import Usage

if TYPE_CHECKING:
    from opentelemetry.metrics import MeterProvider
    from opentelemetry.trace import Span as OtelSpan
    from opentelemetry.trace import Tracer, TracerProvider

    from agentframe.models.base import Model

INSTRUMENTATION_NAME = "agentframe"

UNKNOWN_PROVIDER = "_OTHER"
"""``gen_ai.provider.name`` for a model that does not name its provider."""

type SpanKind = Literal["internal", "client", "server"]
type AttributeValue = str | int | float | bool | tuple[str, ...]
type Attributes = Mapping[str, AttributeValue | None]


@dataclass(frozen=True)
class TracingConfig:
    """Turns on OpenTelemetry spans and metrics for an agent's runs.

    Needs the ``otel`` extra: ``pip install "agentframe-py[otel]"``.

    Attributes:
        tracer_provider: The provider spans are created with, or ``None``
            for OpenTelemetry's global provider.
        meter_provider: The provider metrics are recorded with, or ``None``
            for OpenTelemetry's global provider.
    """

    tracer_provider: "TracerProvider | None" = None
    meter_provider: "MeterProvider | None" = None


class Span:
    """A handle on one span, for setting attributes after it starts.

    The disabled handle ignores everything, so callers never check whether
    tracing is on.

    Attributes:
        skill: The skill the run span's metrics are labelled with, once known.
        error_type: The failure's class name once ``fail`` is called.
    """

    def __init__(self, span: "OtelSpan | None" = None) -> None:
        self._span = span
        self.skill: str | None = None
        self.error_type: str | None = None

    def set(self, key: str, value: AttributeValue | None) -> None:
        """Set an attribute; ``None`` (unknown) sets nothing."""
        if self._span is not None and value is not None:
            self._span.set_attribute(key, value)

    def set_skill(self, skill: str) -> None:
        """Name the skill the run runs, once it is known."""
        self.skill = skill
        self.set("agentframe.skill", skill)

    def fail(self, error: BaseException) -> None:
        """Mark the span as failed with the error's class name, not its message.

        For failures the code handles itself, such as a hosted run that
        reports its error on the task instead of raising it.
        """
        self.error_type = error_type(error)
        if self._span is None:
            return
        from opentelemetry.trace import Status, StatusCode

        self._span.set_attribute("error.type", self.error_type)
        self._span.set_status(Status(StatusCode.ERROR))


class Telemetry:
    """One runtime's tracer and metric instruments, or nothing when disabled."""

    def __init__(self, config: TracingConfig | None) -> None:
        """Create the tracer and instruments for ``config``.

        Raises:
            DefinitionError: ``config`` is given but the ``otel`` extra is
                not installed.
        """
        self._tracer: Tracer | None = None
        if config is None:
            return
        try:
            from opentelemetry import metrics, trace
        except ImportError:
            raise DefinitionError(
                "Tracing needs OpenTelemetry; install 'agentframe-py[otel]'"
            ) from None
        self._tracer = trace.get_tracer(
            INSTRUMENTATION_NAME, tracer_provider=config.tracer_provider
        )
        meter = metrics.get_meter(
            INSTRUMENTATION_NAME, meter_provider=config.meter_provider
        )
        self._runs = meter.create_counter(
            "agentframe.runs", unit="{run}", description="Runs started."
        )
        self._run_errors = meter.create_counter(
            "agentframe.run.errors", unit="{run}", description="Runs that failed."
        )
        self._run_duration = meter.create_histogram(
            "agentframe.run.duration", unit="s", description="Run duration."
        )
        self._rejections = meter.create_counter(
            "agentframe.admission.rejections",
            unit="{request}",
            description="A2A requests refused before a run started.",
        )
        self._token_usage = meter.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="Tokens used by one model call.",
        )
        self._model_duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of one model call, including retries.",
        )

    @property
    def enabled(self) -> bool:
        """Whether spans and metrics are recorded."""
        return self._tracer is not None

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "internal",
        attributes: Attributes | None = None,
        parent: TraceContext | None = None,
    ) -> Iterator[Span]:
        """Record the enclosed code as a span, a child of the current one.

        An exception leaving the block marks the span as failed with the
        exception's class name as ``error.type``; its message is not
        recorded, since it may hold content.

        Args:
            name: The span name.
            kind: The OpenTelemetry span kind.
            attributes: Attributes known at the start; ``None`` values are
                left out.
            parent: A caller's trace context to continue instead of the
                current span, for a run started by a request.
        """
        if self._tracer is None:
            yield Span()
            return
        from opentelemetry.trace import SpanKind as OtelSpanKind

        with self._tracer.start_as_current_span(
            name,
            context=_parent_context(parent),
            kind=OtelSpanKind[kind.upper()],
            attributes=_known(attributes),
            record_exception=False,
            set_status_on_exception=False,
        ) as otel_span:
            span = Span(otel_span)
            try:
                yield span
            except BaseException as error:
                span.fail(error)
                raise

    @contextmanager
    def run(
        self,
        *,
        agent: str,
        invocation_id: str,
        kind: SpanKind = "internal",
        parent: TraceContext | None = None,
    ) -> Iterator[Span]:
        """Record one run as an ``invoke_agent`` span and count it in the metrics.

        The run's skill (``Span.set_skill``) and failure (``Span.fail``, or
        an exception leaving the block) label its metrics.

        Args:
            agent: The agent's name.
            invocation_id: The run's invocation ID, a span attribute only.
            kind: ``"server"`` for a run an A2A request started.
            parent: The request's trace context, continued by the span.
        """
        started = time.perf_counter()
        with self.span(
            f"invoke_agent {agent}",
            kind=kind,
            parent=parent,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": agent,
                "agentframe.invocation_id": invocation_id,
            },
        ) as span:
            try:
                yield span
            except BaseException as error:
                span.fail(error)
                raise
            finally:
                self._record_run(agent, span, time.perf_counter() - started)

    def add_event(self, name: str, attributes: Attributes) -> None:
        """Add an event to the current span."""
        if self._tracer is None:
            return
        from opentelemetry import trace

        trace.get_current_span().add_event(name, _known(attributes))

    def outbound_headers(self, incoming: TraceContext | None) -> dict[str, str]:
        """Return the trace headers to send with a request to another agent.

        With tracing on, they name the current span as the parent, so the
        peer's spans join the same trace. Otherwise the caller's validated
        trace context, if any, goes on unchanged.
        """
        if self._tracer is not None:
            from opentelemetry.trace.propagation.tracecontext import (
                TraceContextTextMapPropagator,
            )

            headers: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(headers)
            if headers:
                return headers
        return incoming.headers() if incoming is not None else {}

    def _record_run(self, agent: str, span: Span, seconds: float) -> None:
        """Count one finished run, its duration and, if it failed, its error."""
        if self._tracer is None:
            return
        attributes = _known(
            {
                "gen_ai.agent.name": agent,
                "agentframe.skill": span.skill,
                "error.type": span.error_type,
            }
        )
        self._runs.add(1, attributes)
        self._run_duration.record(seconds, attributes)
        if span.error_type is not None:
            self._run_errors.add(1, attributes)

    def record_model_call(
        self,
        model: "Model",
        *,
        seconds: float,
        usage: Usage | None,
        error: BaseException | None,
    ) -> None:
        """Record one logical model call's duration and its known token usage."""
        if self._tracer is None:
            return
        attributes = _known(
            {
                **model_attributes(model),
                "error.type": error_type(error) if error is not None else None,
            }
        )
        self._model_duration.record(seconds, attributes)
        if usage is None:
            return
        for token_type, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
        ):
            if count is not None:
                self._token_usage.record(
                    count, {**attributes, "gen_ai.token.type": token_type}
                )

    def record_rejection(self, *, agent: str, reason: str) -> None:
        """Count one A2A request refused before a run started.

        Args:
            agent: The agent's name.
            reason: ``"busy"`` or ``"shutting_down"``.
        """
        if self._tracer is None:
            return
        self._rejections.add(
            1, {"gen_ai.agent.name": agent, "agentframe.rejection.reason": reason}
        )


NO_TELEMETRY = Telemetry(None)
"""Disabled telemetry: records nothing."""


def model_attributes(model: "Model") -> dict[str, AttributeValue]:
    """Return the GenAI attributes naming a chat call to ``model``.

    Models may name themselves with ``provider_name`` and ``model_name``;
    neither is required.
    """
    attributes: dict[str, AttributeValue] = {"gen_ai.operation.name": "chat"}
    provider_name = getattr(model, "provider_name", None)
    attributes["gen_ai.provider.name"] = (
        provider_name if isinstance(provider_name, str) else UNKNOWN_PROVIDER
    )
    model_name = getattr(model, "model_name", None)
    if isinstance(model_name, str):
        attributes["gen_ai.request.model"] = model_name
    return attributes


def error_type(error: BaseException) -> str:
    """Return the class name recorded for ``error``, never its message."""
    return type(error).__name__


def _known(attributes: Attributes | None) -> dict[str, AttributeValue]:
    return {
        key: value for key, value in (attributes or {}).items() if value is not None
    }


def _parent_context(parent: TraceContext | None) -> Any:
    """Return the OpenTelemetry context continuing ``parent``, or ``None``.

    Only W3C trace context is read, never baggage: the headers come from
    the caller.
    """
    if parent is None:
        return None
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    return TraceContextTextMapPropagator().extract(parent.headers())
