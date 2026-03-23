"""Claude Conversation Agent - with device control tools."""
from __future__ import annotations
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

TOOLS = [
    {
        "name": "turn_on",
        "description": "Włącz urządzenie (światło, przełącznik, wentylator itp.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID encji, np. light.salon"},
                "brightness_pct": {"type": "integer", "description": "Jasność 1-100 (tylko dla świateł)"},
                "color_temp_kelvin": {"type": "integer", "description": "Temperatura barwowa w Kelwinach"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "turn_off",
        "description": "Wyłącz urządzenie",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID encji"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "set_temperature",
        "description": "Ustaw temperaturę na termostacie",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "ID encji termostatu"},
                "temperature": {"type": "number", "description": "Temperatura docelowa w °C"},
            },
            "required": ["entity_id", "temperature"],
        },
    },
    {
        "name": "get_states",
        "description": "Pobierz aktualny stan urządzeń z podanej domeny lub konkretnej encji",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domena np. light, climate, sensor, switch"},
                "entity_id": {"type": "string", "description": "Konkretna encja (opcjonalnie)"},
            },
        },
    },
    {
        "name": "call_service",
        "description": "Wywołaj dowolną usługę Home Assistant",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domena usługi np. light, climate, switch"},
                "service": {"type": "string", "description": "Nazwa usługi np. turn_on, set_hvac_mode"},
                "entity_id": {"type": "string", "description": "ID encji"},
                "service_data": {"type": "object", "description": "Dodatkowe dane usługi"},
            },
            "required": ["domain", "service"],
        },
    },
]


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

        reply = await self._run_with_tools(api_key, model, max_tokens, system_prompt, conv_id)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        return ConversationResult(response=intent_response, conversation_id=conv_id)

    async def _run_with_tools(self, api_key, model, max_tokens, system_prompt, conv_id) -> str:
        session = async_get_clientsession(self.hass)

        for _ in range(5):  # max 5 tool call rounds
            try:
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
                        "tools": TOOLS,
                        "messages": self._history[conv_id],
                    },
                ) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        return f"Błąd API {resp.status}: {data}"
            except Exception as err:
                _LOGGER.error("Claude API error: %s", err)
                return f"Błąd połączenia: {err}"

            stop_reason = data.get("stop_reason")
            content = data.get("content", [])

            # Add assistant message to history
            self._history[conv_id].append({"role": "assistant", "content": content})

            if stop_reason != "tool_use":
                # Extract text reply
                for block in content:
                    if block.get("type") == "text":
                        return block["text"]
                return "OK"

            # Execute tools
            tool_results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                tool_name = block["name"]
                tool_input = block.get("input", {})
                tool_use_id = block["id"]

                result = await self._execute_tool(tool_name, tool_input)
                _LOGGER.info("Tool %s(%s) -> %s", tool_name, tool_input, result)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": result,
                })

            self._history[conv_id].append({"role": "user", "content": tool_results})

        return "Przekroczono limit wywołań narzędzi."

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        try:
            if tool_name == "turn_on":
                entity_id = tool_input["entity_id"]
                domain = entity_id.split(".")[0]
                service_data = {"entity_id": entity_id}
                if "brightness_pct" in tool_input:
                    service_data["brightness_pct"] = tool_input["brightness_pct"]
                if "color_temp_kelvin" in tool_input:
                    service_data["color_temp_kelvin"] = tool_input["color_temp_kelvin"]
                await self.hass.services.async_call(domain, "turn_on", service_data)
                return f"Włączono {entity_id}"

            elif tool_name == "turn_off":
                entity_id = tool_input["entity_id"]
                domain = entity_id.split(".")[0]
                await self.hass.services.async_call(domain, "turn_off", {"entity_id": entity_id})
                return f"Wyłączono {entity_id}"

            elif tool_name == "set_temperature":
                await self.hass.services.async_call(
                    "climate", "set_temperature",
                    {"entity_id": tool_input["entity_id"], "temperature": tool_input["temperature"]}
                )
                return f"Ustawiono temperaturę {tool_input['temperature']}°C na {tool_input['entity_id']}"

            elif tool_name == "get_states":
                domain = tool_input.get("domain")
                entity_id = tool_input.get("entity_id")
                lines = []
                for state in self.hass.states.async_all():
                    if entity_id and state.entity_id != entity_id:
                        continue
                    if domain and state.domain != domain:
                        continue
                    name = state.attributes.get("friendly_name", state.entity_id)
                    attrs = {}
                    for k in ("temperature", "current_temperature", "brightness", "hvac_mode", "battery"):
                        if k in state.attributes:
                            attrs[k] = state.attributes[k]
                    lines.append(f"{state.entity_id} ({name}): {state.state} {attrs}")
                return "\n".join(lines) or "Brak encji"

            elif tool_name == "call_service":
                service_data = tool_input.get("service_data", {})
                if "entity_id" in tool_input:
                    service_data["entity_id"] = tool_input["entity_id"]
                await self.hass.services.async_call(
                    tool_input["domain"], tool_input["service"], service_data
                )
                return f"Wywołano {tool_input['domain']}.{tool_input['service']}"

        except Exception as err:
            _LOGGER.error("Tool %s error: %s", tool_name, err)
            return f"Błąd: {err}"

        return "Nieznane narzędzie"

    def _get_states_summary(self) -> str:
        lines = []
        for state in self.hass.states.async_all():
            if state.domain not in ("light", "switch", "climate", "sensor", "binary_sensor", "cover"):
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            name = state.attributes.get("friendly_name", state.entity_id)
            lines.append(f"{name} [{state.entity_id}]: {state.state}")
            if len(lines) >= 80:
                break
        return "\n".join(lines)
