"""Per-run model generation behind ``RunContext.generate``."""

import asyncio
from dataclasses import dataclass

from agentframe.errors import (
    IncompleteOutputError,
    LimitExceededError,
    ModelRefusalError,
    ProviderError,
    UnsupportedCapabilityError,
)
from agentframe.limits import RunLimits
from agentframe.models import Message, Model, ModelRequest, ModelResponse, ModelSettings
from agentframe.usage import FinishReason, Usage

NO_USAGE = Usage(input_tokens=0, output_tokens=0, total_tokens=0, estimated=False)
UNKNOWN_USAGE = Usage(
    input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
)


@dataclass
class GenerationState:
    """One run's model, effective settings and accounting.

    The runtime creates a fresh state for every run, so concurrent runs never
    share usage totals or call counts.

    Attributes:
        model: The agent's model, or ``None`` when it has none.
        instructions: The agent's shared trusted instructions, if any.
        settings: Effective settings for every model call in this run.
        limits: Effective limits for this run.
        usage: Tokens used by this run's model calls so far.
        model_calls: Model calls this run has made so far.
    """

    model: Model | None
    instructions: str | None
    settings: ModelSettings
    limits: RunLimits
    usage: Usage = NO_USAGE
    model_calls: int = 0

    async def generate_text(
        self, text: str, instructions: str | None, deadline: float
    ) -> str:
        """Send ``text`` to the model and return its complete answer.

        Args:
            text: Untrusted content, sent as a ``user`` message.
            instructions: Trusted call-specific instructions, sent after the
                agent's own instructions as a separate ``system`` message.
            deadline: Event-loop time when the run times out.

        Raises:
            UnsupportedCapabilityError: The agent has no model, or the model
                lacks something the request needs.
            ModelRefusalError: The model refused, or a content filter blocked it.
            IncompleteOutputError: The output was cut short or empty.
            ProviderError: The provider call failed.
            LimitExceededError: The run's deadline passed during the call.
        """
        if self.model is None:
            raise UnsupportedCapabilityError("This agent has no model configured")
        messages = build_messages(self.instructions, instructions, text)
        request = ModelRequest(messages=messages, settings=self.settings)
        try:
            async with asyncio.timeout_at(deadline) as timeout:
                response = await self._call(self.model, request)
        except TimeoutError:
            if not timeout.expired():
                raise
            raise LimitExceededError(
                "The run's timeout expired during a model call"
            ) from None
        return text_from(response)

    async def _call(self, model: Model, request: ModelRequest) -> ModelResponse:
        """Call the model, recording the call and its usage even if it fails."""
        try:
            response = await model.generate(request)
        except UnsupportedCapabilityError:
            raise  # Raised before anything was sent, so nothing to record.
        except ProviderError as error:
            self._record(error.usage or UNKNOWN_USAGE)
            raise
        except BaseException:
            # Cancelled or failed mid-call: tokens may have been spent.
            self._record(UNKNOWN_USAGE)
            raise
        self._record(response.usage)
        return response

    def _record(self, usage: Usage) -> None:
        self.model_calls += 1
        self.usage = add_usage(self.usage, usage)


def build_messages(
    agent_instructions: str | None, call_instructions: str | None, text: str
) -> tuple[Message, ...]:
    """Put trusted instructions in ``system`` messages and ``text`` in a ``user`` one.

    Untrusted text is never merged into instructions. This keeps the roles
    separate; it does not guarantee resistance to prompt injection.
    """
    messages = [
        Message(role="system", content=instructions)
        for instructions in (agent_instructions, call_instructions)
        if instructions
    ]
    messages.append(Message(role="user", content=text))
    return tuple(messages)


def text_from(response: ModelResponse) -> str:
    """Return a response's text, or raise when it is not a complete answer.

    Raises:
        IncompleteOutputError: Output stopped at the token limit or is empty.
        ModelRefusalError: The model refused or a content filter blocked it.
    """
    match response.finish_reason:
        case FinishReason.LENGTH:
            raise IncompleteOutputError(
                "The model's output was cut short at its output token limit"
            )
        case FinishReason.REFUSED:
            raise ModelRefusalError("The model refused the request")
        case FinishReason.CONTENT_FILTER:
            raise ModelRefusalError(
                "The model's output was blocked by a content filter"
            )
    if not response.text:
        raise IncompleteOutputError("The model returned no text")
    return response.text


def add_usage(total: Usage, extra: Usage) -> Usage:
    """Add two usages. A count unknown on either side stays unknown."""
    return Usage(
        input_tokens=_add(total.input_tokens, extra.input_tokens),
        output_tokens=_add(total.output_tokens, extra.output_tokens),
        total_tokens=_add(total.total_tokens, extra.total_tokens),
        estimated=total.estimated or extra.estimated,
    )


def _add(count: int | None, extra: int | None) -> int | None:
    if count is None or extra is None:
        return None
    return count + extra
