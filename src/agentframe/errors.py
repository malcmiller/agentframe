"""Errors raised by AgentFrame."""

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
    """The model's output was cut short, for example at its output token limit."""


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


class TimeoutLimitExceededError(LimitExceededError):
    """The run's ``timeout_seconds`` expired."""


class CancelledError(AgentFrameError):
    """A run was cancelled before it finished.

    Distinct from ``asyncio.CancelledError``: this reports a cancelled run to
    callers and does not cancel anything itself.
    """


def describe_validation_error(error: ValidationError) -> str:
    """List field locations and problems without the submitted values.

    Used for safe error messages: the invalid input itself, which may be
    caller or model text, is never included.
    """
    problems = []
    for item in error.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "value"
        problems.append(f"{location}: {item['msg']}")
    return "; ".join(problems)
