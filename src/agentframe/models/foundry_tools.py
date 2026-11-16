"""Foundry hosted tools: tools the Responses API runs on Foundry's side.

Only ``FoundryModel`` supports them; any other model refuses them before
sending. This module needs no optional extra, so skills can name these tools
without importing the ``openai`` package.
"""

from dataclasses import dataclass

from agentframe.errors import DefinitionError
from agentframe.models.base import HostedTool

MAX_CODE_INTERPRETER_FILES = 50
"""Most files a code interpreter container accepts."""

MAX_ALLOWED_DOMAINS = 100
"""Most domains a web search filter accepts."""

MAX_FILE_SEARCH_RESULTS = 50
"""Largest ``max_num_results`` file search accepts."""


@dataclass(frozen=True)
class FoundryCodeInterpreter(HostedTool):
    """Let the model write and run Python in a sandboxed Foundry container.

    Uses an automatic container: Foundry creates one, or reuses one from
    earlier in the conversation, and expires it after 20 idle minutes. Code
    interpreter is billed separately, on top of tokens.

    Attributes:
        file_ids: IDs of uploaded files to make available to the code.

    Raises:
        DefinitionError: ``file_ids`` is a single string or has more than
            50 IDs.
    """

    file_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_ids("FoundryCodeInterpreter.file_ids", self.file_ids)
        if len(self.file_ids) > MAX_CODE_INTERPRETER_FILES:
            raise DefinitionError(
                "FoundryCodeInterpreter.file_ids accepts at most "
                f"{MAX_CODE_INTERPRETER_FILES} files"
            )


@dataclass(frozen=True)
class FoundryWebSearch(HostedTool):
    """Let the model search the web, with Grounding with Bing Search.

    Search queries and results leave Azure's compliance and geographic
    boundaries, and Bing use is billed separately. An administrator can
    block the tool for a subscription.

    Attributes:
        allowed_domains: Domains the search is limited to, such as
            ``"learn.microsoft.com"``. Empty searches every domain.

    Raises:
        DefinitionError: ``allowed_domains`` is a single string or has more
            than 100 domains.
    """

    allowed_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_ids("FoundryWebSearch.allowed_domains", self.allowed_domains)
        if len(self.allowed_domains) > MAX_ALLOWED_DOMAINS:
            raise DefinitionError(
                "FoundryWebSearch.allowed_domains accepts at most "
                f"{MAX_ALLOWED_DOMAINS} domains"
            )


@dataclass(frozen=True)
class FoundryFileSearch(HostedTool):
    """Let the model search uploaded files in Foundry vector stores.

    Attributes:
        vector_store_ids: IDs of the vector stores to search. At least one.
        max_num_results: Most results to return, from 1 to 50, or ``None``
            for the service default.

    Raises:
        DefinitionError: ``vector_store_ids`` is a single string or empty, or
            ``max_num_results`` is outside 1 to 50.
    """

    vector_store_ids: tuple[str, ...]
    max_num_results: int | None = None

    def __post_init__(self) -> None:
        _check_ids("FoundryFileSearch.vector_store_ids", self.vector_store_ids)
        if not self.vector_store_ids:
            raise DefinitionError(
                "FoundryFileSearch needs at least one vector store ID"
            )
        if self.max_num_results is not None and not (
            1 <= self.max_num_results <= MAX_FILE_SEARCH_RESULTS
        ):
            raise DefinitionError(
                "FoundryFileSearch.max_num_results must be from 1 to "
                f"{MAX_FILE_SEARCH_RESULTS}"
            )


def _check_ids(field_name: str, values: object) -> None:
    # A single string would otherwise be read as one ID per character.
    if isinstance(values, str):
        raise DefinitionError(f"{field_name} must be a tuple of strings, not a string")
