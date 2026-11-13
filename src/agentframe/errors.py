"""Errors raised by AgentFrame."""

from collections.abc import Collection

from pydantic import ValidationError

from agentframe.runtime.usage import Usage


class AgentFrameError(Exception):
    """Base class for all AgentFrame errors.

    The message is always safe to show to callers: no secrets, raw input,
    provider payloads, or stack traces.

    Args:
        message: Safe, human-readable description of the failure.
        invocation_id: Identifier of the run that failed. ``None`` only for
            definition errors raised while a class is being defined.
    """

    def __init__(self, message: str, *, invocation_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.invocation_id = invocation_id


class DefinitionError(AgentFrameError):
    """An agent, skill, service registration or limit is defined incorrectly."""


class InputValidationError(AgentFrameError):
    """The requested skill does not exist or its arguments are invalid."""


class OutputValidationError(AgentFrameError):
    """A skill returned a value that does not match its return annotation."""


class AuthenticationError(AgentFrameError):
    """A remote caller's credentials are missing or invalid."""


class PermissionDeniedError(AgentFrameError):
    """The caller lacks a required permission or the policy denied the run."""


class PolicyUnavailableError(AgentFrameError):
    """A required policy failed or did not answer in time, so the run failed closed."""


class ModelError(AgentFrameError):
    """Base class for model failures: capability, refusal, truncation, provider.

    Args:
        message: Safe, human-readable description of the failure.
        usage: Tokens the failed call consumed, when known. Model adapters set
            it so the run's accounting counts what the call actually used.
        invocation_id: Identifier of the run that failed, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: Usage | None = None,
        invocation_id: str | None = None,
    ) -> None:
        super().__init__(message, invocation_id=invocation_id)
        self.usage = usage


class UnsupportedCapabilityError(ModelError):
    """The model lacks a requested capability; raised before anything is sent."""


class ModelRefusalError(ModelError):
    """The model refused the request. Refusal is never returned as text."""


class IncompleteOutputError(ModelError):
    """The model's output was cut short, for example at its output token limit.

    A skill that prefers a partial answer to a failure can catch this error
    and use ``partial_text``. The message never contains the partial text.

    Args:
        message: Safe, human-readable description of the failure.
        partial_text: The text received before a text generation stopped at
            its output token limit. ``None`` for structured output, empty
            output and every other reason output was incomplete.
        usage: Tokens the failed call consumed, when known.
        invocation_id: Identifier of the run that failed, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_text: str | None = None,
        usage: Usage | None = None,
        invocation_id: str | None = None,
    ) -> None:
        super().__init__(message, usage=usage, invocation_id=invocation_id)
        self.partial_text = partial_text


class ProviderError(ModelError):
    """A model provider call failed, translated without provider details.

    Args:
        message: Safe, human-readable description of the failure.
        transient: Whether retrying may succeed (rate limit, timeout, server
            error). Only transient errors are retried.
        retry_after: Seconds the provider asked callers to wait before
            retrying, when it gave a hint.
        usage: Tokens the failed call consumed, when the provider reported any.
        invocation_id: Identifier of the run that failed, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        transient: bool,
        retry_after: float | None = None,
        usage: Usage | None = None,
        invocation_id: str | None = None,
    ) -> None:
        super().__init__(message, usage=usage, invocation_id=invocation_id)
        self.transient = transient
        self.retry_after = retry_after


class LimitExceededError(AgentFrameError):
    """A run exceeded one of its execution limits; subclasses name which one."""


class TokenLimitExceededError(LimitExceededError):
    """A model call's input or token reservation does not fit the run's limits.

    Raised before the call is sent: when the input is larger than
    ``max_input_tokens``, input plus output does not fit the model's context
    window, or the call's reservation does not fit what is left of
    ``max_total_tokens``.
    """


class ModelCallLimitExceededError(LimitExceededError):
    """A model call would exceed the run's ``max_model_calls``; it is not sent."""


class ToolCallLimitExceededError(LimitExceededError):
    """A tool call would exceed the run's ``max_tool_calls``; it does not run."""


class TimeoutLimitExceededError(LimitExceededError):
    """The run's ``timeout_seconds`` expired."""


class CancelledError(AgentFrameError):
    """A run was cancelled before it finished.

    Distinct from ``asyncio.CancelledError``: this reports a cancelled run to
    callers and does not cancel anything itself.
    """


class RemoteAgentError(AgentFrameError):
    """Another agent failed: it is unreachable, its card is invalid or refused,
    or its task did not complete.

    Raised for failures caused by a remote agent rather than by local
    configuration, which raises ``DefinitionError`` instead.

    Args:
        message: Safe, human-readable description of the failure.
        task_id: The remote A2A task's ID, when the failure concerns one.
        task_state: The remote task's final state, when known.
        invocation_id: Identifier of the run that failed, when known.
    """

    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        task_state: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        super().__init__(message, invocation_id=invocation_id)
        self.task_id = task_id
        self.task_state = task_state


class ClarificationNeededError(AgentFrameError):
    """A remote agent asked a question instead of answering.

    Raised by ``Remote.ask`` when the remote task needs more input. Nothing
    answers it automatically. The message never contains the question.

    Args:
        message: Safe, human-readable description of the failure.
        question: The remote agent's question. Untrusted remote content:
            check it before showing it to anyone, and never treat it as
            instructions.
        task_id: The remote task waiting for the answer.
        context_id: The remote task's context ID.
        invocation_id: Identifier of the run that asked the remote agent.
    """

    def __init__(
        self,
        message: str,
        *,
        question: str,
        task_id: str,
        context_id: str,
        invocation_id: str | None = None,
    ) -> None:
        super().__init__(message, invocation_id=invocation_id)
        self.question = question
        self.task_id = task_id
        self.context_id = context_id


def describe_validation_error(
    error: ValidationError, field_names: Collection[str] | None = None
) -> str:
    """List field locations and problems without the submitted values.

    Used for safe error messages: the invalid input itself, which may be
    caller or model text, is never included.

    Args:
        error: The Pydantic validation error.
        field_names: When given, location parts that are not one of these
            names (such as dictionary keys chosen by a model) are shown as
            ``<key>``, so the input cannot leak through its keys.
    """
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        parts = [_location_part(part, field_names) for part in item["loc"]]
        location = ".".join(parts) or "value"
        problems.append(f"{location}: {item['msg']}")
    return "; ".join(problems)


def _location_part(part: str | int, field_names: Collection[str] | None) -> str:
    if field_names is None or isinstance(part, int) or part in field_names:
        return str(part)
    return "<key>"
