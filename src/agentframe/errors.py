"""Errors raised by AgentFrame."""


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
    """The caller lacks a permission that the skill requires."""


class LimitExceededError(AgentFrameError):
    """A run exceeded one of its execution limits."""
