"""Claude Conversation Agent for Home Assistant."""
from __future__ import annotations
import logging
from typing import Literal
import anthropic
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, CONF_MODEL, CONF_MAX_TOKENS, CONF_SYSTEM_PROMPT
from .const import DEFAULT_MODEL, DEFAULT_MAX_TOKENS, DEFAULT_SYSTEM_PROMPT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    agent = ClaudeConversationEntity(hass, entry)
    async_add_entities([agent])


class ClaudeConversationEntity(conversation.ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_languages = MATCH_ALL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._history: dict[str, list] = {}
        self._client = anthropic.AsyncAnthropic(
            api_key=entry.data[CONF_API_KEY]
        )

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        max_tokens = int(self.entry.options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS))
        system_prompt = self.entry.options.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)

        conv_id = user_input.conversation_id or "default"
        if conv_id not in self._history:
            self._history[conv_id] = []

        user_content = user_input.text
        states = self._get_states_summary()
        if states:
            user_content += f"\n\n[Device states]\n{states}"

        self._history[conv_id].append({"role": "user", "content": user_content})
        if len(self._history[conv_id]) > 40:
            self._history[conv_id] = self._history[conv_id][-40:]

        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=self._history[conv_id],
            )
            reply = response.content[0].text
        except anthropic.APIError as err:
            _LOGGER.error("Claude API error: %s", err)
            reply = f"Error: {err}"

        self._history[conv_id].append({"role": "assistant", "content": reply})

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        return conversation.ConversationResult(
            response=intent_response,
            conversation_id=conv_id,
        )

    def _get_states_summary(self) -> str:
        lines = []
        for state in self.hass.states.async_all():
            if state.domain not in ("light","switch","climate","sensor","binary_sensor"):
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            name = state.attributes.get("friendly_name", state.entity_id)
            lines.append(f"{name}: {state.state}")
            if len(lines) >= 60:
                break
        return "\n".join(lines)
