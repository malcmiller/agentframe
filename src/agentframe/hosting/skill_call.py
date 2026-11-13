"""Names of the AgentFrame skill-call extension, shared by hosts and callers.

Kept apart from the card and executor modules so ``Remote`` can use them
without importing the hosting machinery.
"""

SKILL_CALL_EXTENSION_URI = (
    "https://github.com/malcmiller/agentframe/extensions/skill-call/v1"
)
"""The extension a caller activates, per request, to call a skill by ID."""

SKILL_METADATA_KEY = "agentframe.skill"
"""Message metadata key that names the skill to run."""
