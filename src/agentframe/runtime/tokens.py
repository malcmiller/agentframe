"""Token estimates, and the checks that a model call fits the model's limits."""

import json
import math

from agentframe.errors import DefinitionError, TokenLimitExceededError
from agentframe.models.base import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
)
from agentframe.runtime.limits import RunLimits
from agentframe.usage import Usage

CHARACTERS_PER_TOKEN = 4
"""Characters counted as one token when a provider reports no token count.

A common rule of thumb for English text. It is a best-effort estimate, not a
guaranteed bound: some text, such as code or other languages, uses more tokens.
"""


def input_token_limit(limits: RunLimits, capabilities: ModelCapabilities) -> int:
    """Return the most input tokens one call may send.

    That is ``max_input_tokens`` when set, else the model's context window.

    Raises:
        DefinitionError: Neither is known. A context size is never guessed.
    """
    if limits.max_input_tokens is not None:
        return limits.max_input_tokens
    if capabilities.context_window is not None:
        return capabilities.context_window
    raise DefinitionError(
        "The model does not report its context window; set "
        "RunLimits(max_input_tokens=...) or give the model a context window"
    )


def check_fits_model(
    input_tokens: int,
    max_output_tokens: int,
    limits: RunLimits,
    capabilities: ModelCapabilities,
) -> None:
    """Check that a call's input, plus its output cap, fit the model's limits.

    Oversized input fails; it is never truncated, and the output cap is never
    lowered to make room.

    Raises:
        DefinitionError: No input limit is known (see ``input_token_limit``).
        TokenLimitExceededError: The input exceeds the input limit, or input
            plus output exceeds the context window.
    """
    max_input = input_token_limit(limits, capabilities)
    if input_tokens > max_input:
        raise TokenLimitExceededError(
            f"The model input is about {input_tokens} tokens, over the "
            f"limit of {max_input} input tokens"
        )
    context_window = capabilities.context_window
    if context_window is not None and input_tokens + max_output_tokens > context_window:
        raise TokenLimitExceededError(
            f"The model input of about {input_tokens} tokens plus up to "
            f"{max_output_tokens} output tokens exceeds the model's context "
            f"window of {context_window} tokens"
        )


def estimate_tokens(text: str) -> int:
    """Estimate the tokens in ``text`` from its length (``CHARACTERS_PER_TOKEN``)."""
    return math.ceil(len(text) / CHARACTERS_PER_TOKEN)


def estimate_input_tokens(request: ModelRequest) -> int:
    """Estimate the input tokens of ``request``.

    Counts every message, including tool calls and tool results, plus the
    output schema and the tool specs.
    """
    tokens = sum(estimate_message_tokens(message) for message in request.messages)
    if request.output is not None:
        tokens += estimate_tokens(json.dumps(request.output.json_schema))
    for spec in request.tools:
        tokens += estimate_tokens(spec.name + spec.description)
        tokens += estimate_tokens(json.dumps(spec.parameters))
    return tokens


def estimate_message_tokens(message: Message) -> int:
    """Estimate the tokens of one message: its text, tool name and tool calls."""
    tokens = estimate_tokens(message.content + (message.tool_name or ""))
    for call in message.tool_calls:
        tokens += estimate_tokens(call.name + call.arguments)
    return tokens


def estimate_output_tokens(response: ModelResponse) -> int:
    """Estimate the output tokens of ``response``: its text and tool calls."""
    tokens = estimate_tokens(response.text)
    for call in response.tool_calls:
        tokens += estimate_tokens(call.name + call.arguments)
    return tokens


def with_estimates(reported: Usage, input_estimate: int, output_estimate: int) -> Usage:
    """Fill in token counts a provider did not report with estimates.

    Args:
        reported: The usage the model reported; unknown counts are ``None``.
        input_estimate: The estimated input tokens of the call.
        output_estimate: The estimated output tokens of the response.

    Returns:
        Complete usage. ``estimated`` is ``True`` if any count was estimated.
    """
    input_tokens, output_tokens = reported.input_tokens, reported.output_tokens
    if input_tokens is not None and output_tokens is not None:
        if reported.total_tokens is not None:
            return reported
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated=reported.estimated,
        )
    if input_tokens is None:
        input_tokens = input_estimate
    if output_tokens is None:
        output_tokens = output_estimate
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=True,
    )
