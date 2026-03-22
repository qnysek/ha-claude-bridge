"""Constants for Claude Conversation."""
DOMAIN = "claude_conversation"
CONF_MODEL = "model"
CONF_MAX_TOKENS = "max_tokens"
CONF_SYSTEM_PROMPT = "system_prompt"

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_SYSTEM_PROMPT = (
    "You are a smart home assistant integrated with Home Assistant. "
    "Be concise. Answer in the same language as the user. "
    "You have access to the current state of all devices. "
    "When asked to control devices, instruct the user clearly or confirm actions."
)
