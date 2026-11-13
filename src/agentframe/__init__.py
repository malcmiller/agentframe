"""AgentFrame: define agents with Python, communicate through A2A."""

from typing import TYPE_CHECKING

from agentframe.authoring.agent import Agent
from agentframe.authoring.context import RunContext
from agentframe.authoring.decorators import agent, skill, tool
from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
from agentframe.authoring.services import Inject, Services
from agentframe.errors import (
    AgentFrameError,
    AuthenticationError,
    CancelledError,
    DefinitionError,
    IncompleteOutputError,
    InputValidationError,
    LimitExceededError,
    ModelCallLimitExceededError,
    ModelError,
    ModelRefusalError,
    OutputValidationError,
    PermissionDeniedError,
    PolicyUnavailableError,
    ProviderError,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    ToolCallLimitExceededError,
    UnsupportedCapabilityError,
)
from agentframe.hosting.app import AgentApp
from agentframe.hosting.host import HostConfig
from agentframe.hosting.routing import (
    Clarification,
    RoutingContext,
    SkillCall,
    SkillRouter,
)
from agentframe.models.base import (
    Message,
    Model,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    OutputSchema,
    Role,
    ToolCall,
    ToolSpec,
)
from agentframe.runtime.limits import RunLimits
from agentframe.runtime.options import RunOptions
from agentframe.runtime.runtime import RunResult, Runtime
from agentframe.runtime.usage import FinishReason, Usage
from agentframe.security.authentication import (
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
    DeclaresCardSecurity,
    VerifiedCaller,
)
from agentframe.security.authorization import (
    AuthorizationPolicy,
    ToolAuthorizationPolicy,
)
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    from agentframe.models.foundry import FoundryModel as FoundryModel
    from agentframe.models.ollama import OllamaModel as OllamaModel

__version__ = "0.1.0.dev0"

__all__ = [
    "APIKey",
    "APIKeyAuth",
    "Agent",
    "AgentApp",
    "AgentFrameError",
    "AllowAnonymous",
    "AuthenticationError",
    "Authenticator",
    "AuthorizationPolicy",
    "CancelledError",
    "Clarification",
    "DeclaresCardSecurity",
    "DefinitionError",
    "FinishReason",
    "HostConfig",
    "IncompleteOutputError",
    "Inject",
    "InputValidationError",
    "LimitExceededError",
    "Message",
    "Model",
    "ModelCallLimitExceededError",
    "ModelCapabilities",
    "ModelError",
    "ModelRefusalError",
    "ModelRequest",
    "ModelResponse",
    "ModelSettings",
    "OutputSchema",
    "OutputValidationError",
    "PermissionDeniedError",
    "PolicyUnavailableError",
    "Principal",
    "ProviderError",
    "Role",
    "RoutingContext",
    "RunContext",
    "RunLimits",
    "RunOptions",
    "RunResult",
    "Runtime",
    "Services",
    "SkillCall",
    "SkillDefinition",
    "SkillRouter",
    "TimeoutLimitExceededError",
    "TokenLimitExceededError",
    "ToolAuthorizationPolicy",
    "ToolCall",
    "ToolCallLimitExceededError",
    "ToolDefinition",
    "ToolSpec",
    "UnsupportedCapabilityError",
    "Usage",
    "VerifiedCaller",
    "__version__",
    "agent",
    "skill",
    "tool",
]


def __getattr__(name: str) -> object:
    # Provider adapters are imported on first use so that ``import agentframe``
    # works without their optional extras installed.
    if name == "FoundryModel":
        from agentframe.models.foundry import FoundryModel as FoundryModel

        return FoundryModel
    if name == "OllamaModel":
        from agentframe.models.ollama import OllamaModel as OllamaModel

        return OllamaModel
    raise AttributeError(f"module 'agentframe' has no attribute {name!r}")
