"""Claude Conversation Agent - full HA control."""
from __future__ import annotations
import logging
import json as json_lib
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
        "name": "call_service",
        "description": (
            "Wywołaj dowolną usługę Home Assistant. "
            "Użyj do: włączania/wyłączania świateł, przełączników, wentylatorów, "
            "ustawiania temperatury termostatów, odtwarzania muzyki, wysyłania powiadomień itp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domena np. light, climate, switch, media_player, notify, automation"},
                "service": {"type": "string", "description": "Usługa np. turn_on, turn_off, set_temperature, trigger"},
                "service_data": {"type": "object", "description": "Dane usługi np. {entity_id: 'light.salon', brightness_pct: 80}"},
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "get_states",
        "description": "Pobierz stan urządzeń. Filtruj po domenie lub konkretnej encji.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domena np. light, climate, sensor, switch, automation"},
                "entity_id": {"type": "string", "description": "Konkretna encja (opcjonalnie)"},
            },
        },
    },
    {
        "name": "create_automation",
        "description": (
            "Utwórz nową automatyzację w Home Assistant. "
            "Podaj kompletny obiekt automatyzacji w formacie zgodnym z HA YAML/JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alias": {"type": "string", "description": "Nazwa automatyzacji"},
                "description": {"type": "string", "description": "Opis (opcjonalnie)"},
                "trigger": {"type": "array", "description": "Lista wyzwalaczy"},
                "condition": {"type": "array", "description": "Lista warunków (opcjonalnie)"},
                "action": {"type": "array", "description": "Lista akcji"},
                "mode": {"type": "string", "description": "single/parallel/queued/restart", "default": "single"},
            },
            "required": ["alias", "trigger", "action"],
        },
    },
    {
        "name": "list_automations",
        "description": "Wylistuj wszystkie automatyzacje z ich ID, aliasami i statusem.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_automation",
        "description": "Usuń automatyzację po jej entity_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "np. automation.moja_automatyzacja"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "ha_api",
        "description": (
            "Bezpośrednie wywołanie REST API Home Assistant. "
            "Użyj gdy inne narzędzia nie wystarczają. "
            "Np. do pobierania historii, zarządzania integracja, config entries, itp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "GET, POST, DELETE", "default": "GET"},
                "path": {"type": "string", "description": "Ścieżka API np. /api/states, /api/config/config_entries/entry"},
                "body": {"type": "object", "description": "Body dla POST (opcjonalnie)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_config_file",
        "description": (
            "Zapisz lub nadpisz plik konfiguracyjny HA (automations.yaml, scripts.yaml, scenes.yaml itp.). "
            "UWAGA: nadpisuje cały plik."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Nazwa pliku np. automations.yaml, scripts.yaml"},
                "content": {"type": "string", "description": "Pełna zawartość pliku YAML"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "reload_config",
        "description": "Przeładuj konfigurację HA lub konkretną domenę bez restartu.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domena do przeładowania: automation, script, scene, input_boolean, template itp. Zostaw puste dla pełnego reload."},
            },
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

        for _ in range(8):
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
            self._history[conv_id].append({"role": "assistant", "content": content})

            if stop_reason != "tool_use":
                for block in content:
                    if block.get("type") == "text":
                        return block["text"]
                return "Gotowe."

            tool_results = []
            for block in content:
                if block.get("type") != "tool_use":
                    continue
                result = await self._execute_tool(block["name"], block.get("input", {}))
                _LOGGER.info("Tool %s -> %s", block["name"], str(result)[:200])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": str(result),
                })

            self._history[conv_id].append({"role": "user", "content": tool_results})

        return "Przekroczono limit wywołań narzędzi."

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        try:
            # --- call_service ---
            if tool_name == "call_service":
                await self.hass.services.async_call(
                    tool_input["domain"],
                    tool_input["service"],
                    tool_input.get("service_data", {}),
                )
                return f"OK: {tool_input['domain']}.{tool_input['service']}"

            # --- get_states ---
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
                    attrs = {k: v for k, v in state.attributes.items()
                             if k in ("temperature", "current_temperature", "brightness",
                                      "hvac_mode", "battery", "unit_of_measurement",
                                      "friendly_name", "state_class")}
                    lines.append(f"{state.entity_id} ({name}): {state.state} | {attrs}")
                return "\n".join(lines) or "Brak encji"

            # --- create_automation ---
            elif tool_name == "create_automation":
                automation = {
                    "alias": tool_input["alias"],
                    "description": tool_input.get("description", ""),
                    "trigger": tool_input["trigger"],
                    "condition": tool_input.get("condition", []),
                    "action": tool_input["action"],
                    "mode": tool_input.get("mode", "single"),
                }
                result = await self.hass.services.async_call(
                    "automation", "reload", {}, blocking=True
                )
                # Write to automations.yaml
                import os, yaml  # yaml is available in HA
                auto_file = "/config/automations.yaml"
                existing = []
                if os.path.exists(auto_file):
                    with open(auto_file) as f:
                        existing = yaml.safe_load(f) or []
                existing.append(automation)
                with open(auto_file, "w") as f:
                    yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
                await self.hass.services.async_call("automation", "reload", {}, blocking=True)
                return f"Utworzono automatyzację: {tool_input['alias']}"

            # --- list_automations ---
            elif tool_name == "list_automations":
                lines = []
                for state in self.hass.states.async_all():
                    if state.domain != "automation":
                        continue
                    name = state.attributes.get("friendly_name", state.entity_id)
                    lines.append(f"{state.entity_id}: {name} [{state.state}]")
                return "\n".join(lines) or "Brak automatyzacji"

            # --- delete_automation ---
            elif tool_name == "delete_automation":
                entity_id = tool_input["entity_id"]
                import os, yaml
                auto_file = "/config/automations.yaml"
                if os.path.exists(auto_file):
                    with open(auto_file) as f:
                        existing = yaml.safe_load(f) or []
                    # Get friendly name from state
                    state = self.hass.states.get(entity_id)
                    alias = state.attributes.get("friendly_name", "") if state else ""
                    existing = [a for a in existing if a.get("alias", "") != alias]
                    with open(auto_file, "w") as f:
                        yaml.dump(existing, f, allow_unicode=True, default_flow_style=False)
                    await self.hass.services.async_call("automation", "reload", {}, blocking=True)
                    return f"Usunięto: {entity_id}"
                return "Plik automations.yaml nie istnieje"

            # --- ha_api ---
            elif tool_name == "ha_api":
                session = async_get_clientsession(self.hass)
                from homeassistant.helpers.network import get_url
                base_url = get_url(self.hass, prefer_external=False)
                method = tool_input.get("method", "GET").upper()
                url = base_url + tool_input["path"]
                token = self.entry.data[CONF_API_KEY]
                # Use internal HA token from context
                from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
                headers = {"Authorization": f"Bearer {self.entry.data[CONF_API_KEY]}",
                           "Content-Type": "application/json"}
                # Better: use HA internal API directly
                if method == "GET":
                    resp_obj = await session.get(url, headers=headers)
                elif method == "POST":
                    resp_obj = await session.post(url, headers=headers, json=tool_input.get("body", {}))
                elif method == "DELETE":
                    resp_obj = await session.delete(url, headers=headers)
                else:
                    return f"Nieznana metoda: {method}"
                text = await resp_obj.text()
                return text[:2000]

            # --- write_config_file ---
            elif tool_name == "write_config_file":
                filename = tool_input["filename"]
                # Security: only allow config files
                allowed = ("automations.yaml", "scripts.yaml", "scenes.yaml",
                           "groups.yaml", "customize.yaml", "input_boolean.yaml",
                           "input_number.yaml", "input_text.yaml", "input_select.yaml",
                           "configuration.yaml")
                if filename not in allowed:
                    return f"Niedozwolony plik: {filename}. Dozwolone: {allowed}"
                path = f"/config/{filename}"
                with open(path, "w") as f:
                    f.write(tool_input["content"])
                return f"Zapisano {path}"

            # --- reload_config ---
            elif tool_name == "reload_config":
                domain = tool_input.get("domain")
                if domain:
                    await self.hass.services.async_call(domain, "reload", {}, blocking=True)
                    return f"Przeładowano: {domain}"
                else:
                    await self.hass.services.async_call("homeassistant", "reload_all", {}, blocking=True)
                    return "Przeładowano całą konfigurację"

        except Exception as err:
            _LOGGER.error("Tool %s error: %s", tool_name, err)
            return f"Błąd narzędzia {tool_name}: {err}"

        return "Nieznane narzędzie"

    def _get_states_summary(self) -> str:
        lines = []
        for state in self.hass.states.async_all():
            if state.domain not in ("light", "switch", "climate", "sensor",
                                    "binary_sensor", "cover", "automation", "script"):
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            name = state.attributes.get("friendly_name", state.entity_id)
            lines.append(f"{name} [{state.entity_id}]: {state.state}")
            if len(lines) >= 100:
                break
        return "\n".join(lines)
