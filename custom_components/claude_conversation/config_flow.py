"""Config flow for Claude Conversation."""
from __future__ import annotations
import anthropic
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.selector import (
    TextSelector, TextSelectorConfig, TextSelectorType,
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
)
from .const import DOMAIN, CONF_MODEL, CONF_MAX_TOKENS, CONF_SYSTEM_PROMPT, DEFAULT_MODEL, DEFAULT_MAX_TOKENS, DEFAULT_SYSTEM_PROMPT


class ClaudeConversationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client = anthropic.AsyncAnthropic(api_key=user_input[CONF_API_KEY])
                await client.models.list()
            except anthropic.AuthenticationError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Claude", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(entry):
        return ClaudeOptionsFlow(entry)


class ClaudeOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry):
        self._entry = entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_MODEL, default=self._entry.options.get(CONF_MODEL, DEFAULT_MODEL)):
                    TextSelector(),
                vol.Optional(CONF_MAX_TOKENS, default=self._entry.options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)):
                    NumberSelector(NumberSelectorConfig(min=256, max=4096, step=256, mode=NumberSelectorMode.SLIDER)),
                vol.Optional(CONF_SYSTEM_PROMPT, default=self._entry.options.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)):
                    TextSelector(TextSelectorConfig(multiline=True)),
            }),
        )
