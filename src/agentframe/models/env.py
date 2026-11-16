"""``model_from_env``: choose a model provider with environment variables.

Switching between local Llama and a Foundry deployment then changes only the
environment, not code. Each provider is imported only when selected, so an
install with just one provider's extra works.
"""

import os
from typing import TYPE_CHECKING

from agentframe.errors import DefinitionError
from agentframe.models.base import Model

if TYPE_CHECKING:
    from azure.core.credentials_async import AsyncTokenCredential

DEFAULT_OLLAMA_CONTEXT_WINDOW = 8192
"""Context window requested from Ollama when none is configured.

Ollama allocates memory for the whole window, and a model's maximum is often
far larger than an agent needs (see ``OllamaModel``).
"""


def model_from_env(
    prefix: str = "AGENTFRAME_",
    *,
    credential: "str | AsyncTokenCredential | None" = None,
) -> Model:
    """Return the model that ``<prefix>MODEL_PROVIDER`` selects.

    Reads these variables, each name starting with ``prefix``:

    - ``MODEL_PROVIDER``: ``ollama`` or ``foundry``.
    - ``OLLAMA_MODEL``: tag of an installed model (Ollama).
    - ``OLLAMA_ENDPOINT``: server URL, optional (Ollama); defaults to
      ``http://localhost:11434``.
    - ``FOUNDRY_ENDPOINT``: OpenAI v1 base URL (Foundry).
    - ``FOUNDRY_DEPLOYMENT``: deployment name (Foundry).
    - ``MODEL_CONTEXT_WINDOW``: tokens of input plus output, optional. Ollama
      defaults to 8192; Foundry defaults to unknown.

    An empty variable counts as unset. Nothing is read at import time and the
    call does no network I/O. Error messages name the variable but never
    include its value.

    Args:
        prefix: Prefix of every variable name, so several models can be
            configured side by side, for example ``"PLANNER_"``.
        credential: Foundry only: an API key or an async Azure token
            credential, which stays yours. ``None`` signs in with a new
            ``DefaultAzureCredential`` that the model owns and closes with
            itself. Ignored for Ollama, which has no authentication.

    Returns:
        A new ``OllamaModel`` or ``FoundryModel``. The application owns it and
        closes it when done.

    Raises:
        DefinitionError: A required variable is unset, the provider is
            unknown, or the context window is not a positive whole number.
        ImportError: The selected provider's optional extra is not installed.
    """
    provider = _required(prefix, "MODEL_PROVIDER")
    context_window = _context_window(prefix)
    if provider == "ollama":
        tag = _required(prefix, "OLLAMA_MODEL")
        endpoint = _optional(prefix, "OLLAMA_ENDPOINT")
        from agentframe.models.ollama import DEFAULT_ENDPOINT, OllamaModel

        return OllamaModel(
            model=tag,
            endpoint=endpoint or DEFAULT_ENDPOINT,
            context_window=context_window or DEFAULT_OLLAMA_CONTEXT_WINDOW,
        )
    if provider == "foundry":
        endpoint = _required(prefix, "FOUNDRY_ENDPOINT")
        deployment = _required(prefix, "FOUNDRY_DEPLOYMENT")
        from agentframe.models.foundry import FoundryModel, default_credential_model

        if credential is None:
            return default_credential_model(
                endpoint=endpoint, deployment=deployment, context_window=context_window
            )
        return FoundryModel(
            endpoint=endpoint,
            deployment=deployment,
            credential=credential,
            context_window=context_window,
        )
    raise DefinitionError(f"{prefix}MODEL_PROVIDER must be 'ollama' or 'foundry'")


def _optional(prefix: str, name: str) -> str | None:
    return os.environ.get(prefix + name) or None


def _required(prefix: str, name: str) -> str:
    value = _optional(prefix, name)
    if value is None:
        raise DefinitionError(f"Set the {prefix}{name} environment variable")
    return value


def _context_window(prefix: str) -> int | None:
    value = _optional(prefix, "MODEL_CONTEXT_WINDOW")
    if value is None:
        return None
    # isdecimal() refuses signs, so a parsed value can only be 0 or more.
    if not value.isdecimal() or int(value) == 0:
        raise DefinitionError(
            f"{prefix}MODEL_CONTEXT_WINDOW must be a whole number greater than 0"
        )
    return int(value)
