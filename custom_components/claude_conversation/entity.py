"""Claude Conversation Agent - uses aiohttp instead of anthropic SDK."""
from __future__ import annotations
import json
import logging
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_MODEL, CONF_MAX_TOKENS, CONF_SYSTEM_PROMPT
from .const import DEFAULT_MODEL, DEFAULT_MAX_TOKENS, DEFAULT_SYSTEM_PROMPT

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.conversation import (
        ConversationEntity, ConversationInput, ConversationResult,
    )
except ImportError:
    from homeassistant.components.conversation import ConversationEntity
    from homeassistant.components.conversation.models import (
        ConversationInput, ConversationResult,
    )

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


class ClaudeConversationEntity(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._history: dict[str, list] = {}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return MATCH_ALL

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        api_key = self.entry.data[CONF_API_KEY]
        model = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        max_tokens = int(self.entry.options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS))
        system_prompt = self.entry.options.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)

        conv_id = user_input.conversation_id or "default"
        if conv_id not in self._history:
            self._history[conv_id] = []

        user_content = user_input.text
        states = self._get_states_summary()
        if states:
            user_content += f"\n\n[Stan urządzeń]\n{states}"

        self._history[conv_id].append({"role": "user", "content": user_content})
        if len(self._history[conv_id]) > 40:
            self._history[conv_id] = self._history[conv_id][-40:]

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": self._history[conv_id],
                },
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise Exception(f"API {resp.status}: {data}")
                reply = data["content"][0]["text"]
        except Exception as err:
            _LOGGER.error("Claude API error: %s", err)
            reply = f"Błąd: {err}"

        self._history[conv_id].append({"role": "assistant", "content": reply})

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        return ConversationResult(response=intent_response, conversation_id=conv_id)

    def _get_states_summary(self) -> str:
        lines = []
        for state in self.hass.states.async_all():
            if state.domain not in ("light", "switch", "climate", "sensor", "binary_sensor", "cover"):
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            name = state.attributes.get("friendly_name", state.entity_id)
            lines.append(f"{name}: {state.state}")
            if len(lines) >= 80:
                break
        return "\n".join(lines)
