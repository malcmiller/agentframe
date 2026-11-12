"""``FoundryModel``: an adapter for GPT deployments in Microsoft Foundry.

Calls the OpenAI v1 Responses API through the official ``openai`` package.
Needs the optional extra: ``pip install agentframe[foundry]``.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Self

try:
    import openai
    from azure.core.credentials_async import AsyncTokenCredential
    from azure.core.exceptions import ClientAuthenticationError
    from azure.identity.aio import get_bearer_token_provider
    from openai.types.responses import (
        EasyInputMessageParam,
        Response,
        ResponseInputParam,
        ResponseUsage,
    )
except ImportError as error:
    raise ImportError(
        "FoundryModel needs the optional 'openai' and 'azure-identity' packages; "
        "install them with: pip install 'agentframe[foundry]'"
    ) from error

from agentframe.errors import DefinitionError, IncompleteOutputError, ProviderError
from agentframe.models.base import (
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
)
from agentframe.runtime.usage import FinishReason, Usage

FOUNDRY_SCOPE = "https://ai.azure.com/.default"
"""Microsoft Entra scope for Foundry inference tokens."""

TRANSIENT_FAILURE_CODES = frozenset({"server_error", "rate_limit_exceeded"})
"""Codes of ``failed`` responses that retrying may fix."""


class FoundryModel:
    """A GPT deployment in Microsoft Foundry, called through the Responses API.

    Give either ``endpoint`` and ``credential``, in which case the model creates
    and owns its client, or an existing ``client``, which stays caller-owned.
    Construction does no I/O. SDK retries are always disabled, so AgentFrame
    is the only retry layer.

    The API does not report a deployment's context window, so it is unknown
    unless ``context_window`` is given. Unknown is never guessed.

    ``max_output_tokens`` caps visible output plus reasoning tokens, so
    reasoning models need a larger cap than their visible answer.

    Credentials are never logged or included in error messages.

    Args:
        deployment: Name of the model deployment, for example ``"gpt-5.4"``.
        endpoint: OpenAI v1 base URL, such as
            ``"https://<resource>.openai.azure.com/openai/v1/"``.
        credential: An API key, or an async Azure token credential such as
            ``azure.identity.aio.AzureCliCredential()``. A token credential
            stays caller-owned; the model never closes it. Production hosts
            can use ``DefaultAzureCredential()`` or managed identity; the
            ``foundry`` extra installs the ``aiohttp`` transport they need.
        client: An existing ``openai.AsyncOpenAI`` client to use instead of
            ``endpoint`` and ``credential``. It is used with retries disabled
            and is never closed by the model.
        context_window: Tokens of input plus output the deployment accepts,
            or ``None`` when unknown.
        store: Whether Foundry stores responses server-side. This controls
            response storage only, not every provider retention mechanism.

    Raises:
        DefinitionError: If neither or both of ``client`` and
            ``endpoint``/``credential`` are given, or ``context_window`` is not
            greater than zero.
    """

    def __init__(
        self,
        *,
        deployment: str,
        endpoint: str | None = None,
        credential: str | AsyncTokenCredential | None = None,
        client: openai.AsyncOpenAI | None = None,
        context_window: int | None = None,
        store: bool = False,
    ) -> None:
        if context_window is not None and context_window <= 0:
            raise DefinitionError("FoundryModel context_window must be greater than 0")
        self.deployment = deployment
        self.store = store
        self._context_window = context_window
        self._owns_client = client is None
        self._client = _resolve_client(endpoint, credential, client)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send ``request`` to the Responses API and return the full reply.

        Returns:
            The reply. A refusal finishes ``REFUSED`` with empty text; output
            stopped at the token cap finishes ``LENGTH``, and output stopped by
            a content filter finishes ``CONTENT_FILTER``.

        Raises:
            IncompleteOutputError: The response stopped early for another reason.
            ProviderError: The call or the response failed. Connection failures,
                timeouts, rate limits and server errors are transient; rejected
                credentials, an unknown deployment and invalid requests are not.
        """
        instructions, conversation = _split_instructions(request.messages)
        async with _provider_errors(self.deployment):
            response = await self._client.responses.create(
                model=self.deployment,
                instructions=instructions,
                input=conversation,
                max_output_tokens=request.settings.max_output_tokens,
                store=self.store,
            )
        return _model_response(response)

    async def capabilities(self) -> ModelCapabilities:
        """Return the configured context window; structured output and tools are off.

        Does no I/O: the API does not report a deployment's capabilities.
        """
        return ModelCapabilities(context_window=self._context_window)

    async def close(self) -> None:
        """Close the client this model created; an injected client stays open."""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        """Return the model itself."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the model."""
        await self.close()


def _resolve_client(
    endpoint: str | None,
    credential: str | AsyncTokenCredential | None,
    client: openai.AsyncOpenAI | None,
) -> openai.AsyncOpenAI:
    if client is not None:
        if endpoint is not None or credential is not None:
            raise DefinitionError(
                "Give FoundryModel either client= or endpoint= and credential=, "
                "not both"
            )
        # A copy with retries off; it shares, and never closes, the caller's
        # HTTP connections.
        return client.with_options(max_retries=0)
    if endpoint is None or credential is None:
        raise DefinitionError(
            "FoundryModel needs endpoint= and credential=, or an existing client="
        )
    api_key = (
        credential
        if isinstance(credential, str)
        else get_bearer_token_provider(credential, FOUNDRY_SCOPE)
    )
    return openai.AsyncOpenAI(base_url=endpoint, api_key=api_key, max_retries=0)


def _split_instructions(
    messages: tuple[Message, ...],
) -> tuple[str | None, ResponseInputParam]:
    """Send leading system messages as ``instructions``, the rest as input.

    A later system message keeps its place in the conversation as a
    ``developer`` message, the Responses API role for trusted instructions.
    """
    leading = 0
    while leading < len(messages) and messages[leading].role == "system":
        leading += 1
    instructions = "\n\n".join(message.content for message in messages[:leading])
    conversation: ResponseInputParam = [
        EasyInputMessageParam(
            role="developer" if message.role == "system" else message.role,
            content=message.content,
        )
        for message in messages[leading:]
    ]
    return instructions or None, conversation


def _model_response(response: Response) -> ModelResponse:
    """Map a Responses API result to a ``ModelResponse``, failing on error states."""
    usage = _usage(response.usage)
    if response.status not in ("completed", "incomplete"):
        raise _unfinished_response_error(response, usage)
    if _has_refusal(response):
        return ModelResponse(text="", usage=usage, finish_reason=FinishReason.REFUSED)
    finish_reason = FinishReason.COMPLETED
    if response.status == "incomplete":
        finish_reason = _incomplete_finish_reason(response, usage)
    return ModelResponse(
        text=response.output_text, usage=usage, finish_reason=finish_reason
    )


def _unfinished_response_error(response: Response, usage: Usage) -> ProviderError:
    if response.status == "failed":
        code = response.error.code if response.error else None
        return ProviderError(
            f"Foundry reported a failed response (code {code or 'unknown'})",
            transient=code in TRANSIENT_FAILURE_CODES,
            usage=usage,
        )
    if response.status == "cancelled":
        return ProviderError(
            "Foundry cancelled the response", transient=False, usage=usage
        )
    # queued and in_progress only happen for background responses, never sent.
    return ProviderError(
        f"Foundry returned an unfinished response (status {response.status})",
        transient=False,
        usage=usage,
    )


def _has_refusal(response: Response) -> bool:
    return any(
        content.type == "refusal"
        for item in response.output
        if item.type == "message"
        for content in item.content
    )


def _incomplete_finish_reason(response: Response, usage: Usage) -> FinishReason:
    reason = response.incomplete_details.reason if response.incomplete_details else None
    if reason == "max_output_tokens":
        return FinishReason.LENGTH
    if reason == "content_filter":
        return FinishReason.CONTENT_FILTER
    raise IncompleteOutputError(
        f"Foundry stopped the response early (reason {reason or 'unknown'})",
        usage=usage,
    )


def _usage(usage: ResponseUsage | None) -> Usage:
    if usage is None:
        return Usage(
            input_tokens=None, output_tokens=None, total_tokens=None, estimated=False
        )
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated=False,
    )


@asynccontextmanager
async def _provider_errors(deployment: str) -> AsyncIterator[None]:
    """Turn client and credential errors into ``ProviderError`` with a safe message.

    The service's own error text is not passed on because it may echo request
    content back to the caller.
    """
    try:
        yield
    except openai.APIConnectionError as error:  # Includes timeouts.
        raise ProviderError("Could not reach Foundry", transient=True) from error
    except openai.APIStatusError as error:
        raise _status_error(error, deployment) from error
    except ClientAuthenticationError as error:
        raise ProviderError(
            "Could not get a Microsoft Entra token for Foundry", transient=False
        ) from error
    except openai.OpenAIError as error:
        raise ProviderError("The Foundry call failed", transient=False) from error


def _status_error(error: openai.APIStatusError, deployment: str) -> ProviderError:
    status_code = error.status_code
    if status_code in (401, 403):
        return ProviderError(
            f"Foundry rejected the credentials (HTTP {status_code})", transient=False
        )
    if status_code == 404:
        return ProviderError(
            f"Foundry deployment {deployment!r} was not found", transient=False
        )
    if status_code in (408, 429) or status_code >= 500:
        return ProviderError(
            f"Foundry could not handle the call now (HTTP {status_code})",
            transient=True,
            retry_after=_retry_after(error.response.headers),
        )
    return ProviderError(
        f"Foundry rejected the request (HTTP {status_code})", transient=False
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    """Read the retry hint in seconds, preferring ``retry-after-ms``.

    Azure OpenAI sends ``retry-after-ms`` in milliseconds alongside the
    standard ``Retry-After`` in seconds. HTTP dates are ignored.
    """
    milliseconds = _number(headers.get("retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000
    return _number(headers.get("retry-after"))


def _number(header: str | None) -> float | None:
    try:
        return float(header) if header is not None else None
    except ValueError:
        return None
