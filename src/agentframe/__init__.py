"""AgentFrame: define agents with Python, communicate through A2A."""

from typing import TYPE_CHECKING

from agentframe.authoring.agent import Agent
from agentframe.authoring.context import RunContext
from agentframe.authoring.decorators import agent, skill, tool
from agentframe.authoring.definitions import SkillDefinition, ToolDefinition
from agentframe.authoring.services import Inject, Services
from agentframe.discovery.registry import (
    AgentQuery,
    AgentReference,
    AgentRegistry,
    EnvironmentAgentRegistry,
    FileAgentRegistry,
)
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
from agentframe.hosting.dedup import MessageDedupStore
from agentframe.hosting.host import A2AConfig, HostConfig
from agentframe.hosting.model_router import ModelRouter
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
from agentframe.security.execution_policy import (
    ExecutionPolicy,
    InputCheck,
    ModelRequestCheck,
    OutputCheck,
    ToolCheck,
)
from agentframe.security.principal import Principal

if TYPE_CHECKING:
    from agentframe.models.foundry import FoundryModel as FoundryModel
    from agentframe.models.ollama import OllamaModel as OllamaModel
    from agentframe.security.entra import EntraIDAuth as EntraIDAuth

__version__ = "0.1.0"

__all__ = [
    "A2AConfig",
    "APIKey",
    "APIKeyAuth",
    "Agent",
    "AgentApp",
    "AgentFrameError",
    "AgentQuery",
    "AgentReference",
    "AgentRegistry",
    "AllowAnonymous",
    "AuthenticationError",
    "Authenticator",
    "AuthorizationPolicy",
    "CancelledError",
    "Clarification",
    "DeclaresCardSecurity",
    "DefinitionError",
    "EnvironmentAgentRegistry",
    "ExecutionPolicy",
    "FileAgentRegistry",
    "FinishReason",
    "HostConfig",
    "IncompleteOutputError",
    "Inject",
    "InputCheck",
    "InputValidationError",
    "LimitExceededError",
    "Message",
    "MessageDedupStore",
    "Model",
    "ModelCallLimitExceededError",
    "ModelCapabilities",
    "ModelError",
    "ModelRefusalError",
    "ModelRequest",
    "ModelRequestCheck",
    "ModelResponse",
    "ModelRouter",
    "ModelSettings",
    "OutputCheck",
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
    "ToolCheck",
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
    # Provider adapters and EntraIDAuth are imported on first use so that
    # ``import agentframe`` works without their optional extras installed.
    if name == "FoundryModel":
        from agentframe.models.foundry import FoundryModel as FoundryModel

        return FoundryModel
    if name == "OllamaModel":
        from agentframe.models.ollama import OllamaModel as OllamaModel

        return OllamaModel
    if name == "EntraIDAuth":
        from agentframe.security.entra import EntraIDAuth as EntraIDAuth

        return EntraIDAuth
    raise AttributeError(f"module 'agentframe' has no attribute {name!r}")
