"""Claude Conversation Agent - full HA control v3."""
from __future__ import annotations
import logging
import os
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
CONFIG_DIR = "/config"
ALLOWED_FILES = (
    "automations.yaml", "scripts.yaml", "scenes.yaml",
    "groups.yaml", "customize.yaml", "input_boolean.yaml",
    "input_number.yaml", "input_text.yaml", "input_select.yaml",
    "input_datetime.yaml", "configuration.yaml", "lovelace.yaml",
)

TOOLS = [
    {
        "name": "call_service",
        "description": "Wywołaj dowolną usługę Home Assistant. Służy do sterowania urządzeniami, włączania/wyłączania automatyzacji, wysyłania powiadomień itp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "service": {"type": "string"},
                "service_data": {"type": "object"},
            },
            "required": ["domain", "service"],
        },
    },
    {
        "name": "get_states",
        "description": "Pobierz stan encji. Filtruj po domenie lub entity_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "entity_id": {"type": "string"},
            },
        },
    },
    {
        "name": "read_file",
        "description": "Odczytaj plik konfiguracyjny HA (automations.yaml, scripts.yaml, configuration.yaml itp.).",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "np. automations.yaml"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "write_file",
        "description": "Zapisz lub nadpisz plik konfiguracyjny HA. Użyj do tworzenia i edycji automatyzacji, skryptów, scen, konfiguracji Lovelace itp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "content": {"type": "string", "description": "Pełna zawartość pliku YAML"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "reload_domain",
        "description": "Przeładuj konfigurację domeny bez restartu HA. Użyj po zapisie pliku.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "automation, script, scene, input_boolean, lovelace, homeassistant itp."},
            },
            "required": ["domain"],
        },
    },
    {
        "name": "supervisor_api",
        "description": "Wywołaj Supervisor API - zarządzanie addonami (start/stop/restart/info), systemu, sieci.",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "GET lub POST", "default": "GET"},
                "path": {"type": "string", "description": "np. /addons, /addons/a0d7b954_nodered/info, /addons/a0d7b954_nodered/restart"},
                "body": {"type": "object"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "ha_rest_api",
        "description": "Bezpośrednie wywołanie REST API HA. Do integracji, historii, logbook, config entries itp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "default": "GET"},
                "path": {"type": "string", "description": "np. /api/config/config_entries/entry, /api/history/period"},
                "body": {"type": "object"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_addons",
        "description": "Wylistuj zainstalowane addony z ich statusem.",
        "input_schema": {"type": "object", "properties": {}},
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
        states_summary = self._get_states_summary()
        if states_summary:
            user_content += f"\n\n[Stan urządzeń]\n{states_summary}"

        self._history[conv_id].append({"role": "user", "content": user_content})
        if len(self._history[conv_id]) > 40:
            self._history[conv_id] = self._history[conv_id][-40:]

        reply = await self._run_with_tools(api_key, model, max_tokens, system_prompt, conv_id)

        intent_response = intent.IntentResponse(language=user_input.language)
        intent_response.async_set_speech(reply)
        return ConversationResult(response=intent_response, conversation_id=conv_id)

    async def _run_with_tools(self, api_key, model, max_tokens, system_prompt, conv_id) -> str:
        session = async_get_clientsession(self.hass)
        for _ in range(10):
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
                    timeout=60,
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
                _LOGGER.info("Tool %s(%s) -> %s", block["name"], block.get("input",{}), str(result)[:300])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": str(result),
                })
            self._history[conv_id].append({"role": "user", "content": tool_results})

        return "Przekroczono limit wywołań narzędzi."

    async def _execute_tool(self, tool_name: str, inp: dict) -> str:
        try:
            if tool_name == "call_service":
                await self.hass.services.async_call(
                    inp["domain"], inp["service"], inp.get("service_data", {}), blocking=True
                )
                return f"OK: {inp['domain']}.{inp['service']}"

            elif tool_name == "get_states":
                domain = inp.get("domain")
                entity_id = inp.get("entity_id")
                lines = []
                for state in self.hass.states.async_all():
                    if entity_id and state.entity_id != entity_id:
                        continue
                    if domain and state.domain != domain:
                        continue
                    name = state.attributes.get("friendly_name", state.entity_id)
                    attrs = {k: v for k, v in state.attributes.items()
                             if k in ("temperature", "current_temperature", "brightness",
                                      "hvac_mode", "hvac_action", "battery", "unit_of_measurement",
                                      "preset_mode", "fan_mode", "humidity", "device_class")}
                    lines.append(f"{state.entity_id} ({name}): {state.state} {attrs}")
                return "\n".join(lines) or "Brak encji"

            elif tool_name == "read_file":
                filename = os.path.basename(inp["filename"])
                path = os.path.join(CONFIG_DIR, filename)
                if not os.path.exists(path):
                    return f"Plik nie istnieje: {path}"
                with open(path, "r") as f:
                    content = f.read()
                return content[:8000]

            elif tool_name == "write_file":
                filename = os.path.basename(inp["filename"])
                if filename not in ALLOWED_FILES:
                    return f"Niedozwolony plik. Dozwolone: {ALLOWED_FILES}"
                path = os.path.join(CONFIG_DIR, filename)
                with open(path, "w") as f:
                    f.write(inp["content"])
                return f"Zapisano: {path} ({len(inp['content'])} znaków)"

            elif tool_name == "reload_domain":
                domain = inp["domain"]
                reload_services = {
                    "automation": ("automation", "reload"),
                    "script": ("script", "reload"),
                    "scene": ("scene", "reload"),
                    "lovelace": ("lovelace", "reload_resources"),
                    "input_boolean": ("input_boolean", "reload"),
                    "input_number": ("input_number", "reload"),
                    "input_text": ("input_text", "reload"),
                    "input_select": ("input_select", "reload"),
                    "input_datetime": ("input_datetime", "reload"),
                    "homeassistant": ("homeassistant", "reload_all"),
                    "template": ("template", "reload"),
                    "group": ("group", "reload"),
                }
                if domain in reload_services:
                    svc_domain, svc = reload_services[domain]
                    await self.hass.services.async_call(svc_domain, svc, {}, blocking=True)
                else:
                    await self.hass.services.async_call(domain, "reload", {}, blocking=True)
                return f"Przeładowano: {domain}"

            elif tool_name == "list_addons":
                session = async_get_clientsession(self.hass)
                token = self.entry.data[CONF_API_KEY]
                from homeassistant.helpers.network import get_url
                base = get_url(self.hass, prefer_external=False)
                async with session.get(
                    f"{base}/api/hassio/addons",
                    headers={"Authorization": f"Bearer {token}"}
                ) as r:
                    if r.status == 401:
                        # Try via supervisor token from env
                        import os as _os
                        sup_token = _os.environ.get("SUPERVISOR_TOKEN", "")
                        async with session.get(
                            "http://supervisor/addons",
                            headers={"Authorization": f"Bearer {sup_token}"}
                        ) as r2:
                            data = await r2.json()
                    else:
                        data = await r.json()
                addons = data.get("data", {}).get("addons", [])
                lines = [f"{a['slug']}: {a['name']} [{a['state']}]" for a in addons]
                return "\n".join(lines) or "Brak addonów"

            elif tool_name == "supervisor_api":
                import os as _os
                sup_token = _os.environ.get("SUPERVISOR_TOKEN", "")
                method = inp.get("method", "GET").upper()
                path = inp["path"].lstrip("/")
                url = f"http://supervisor/{path}"
                session = async_get_clientsession(self.hass)
                headers = {"Authorization": f"Bearer {sup_token}"}
                if method == "GET":
                    async with session.get(url, headers=headers) as r:
                        return (await r.text())[:3000]
                else:
                    async with session.post(url, headers=headers, json=inp.get("body", {})) as r:
                        return (await r.text())[:3000]

            elif tool_name == "ha_rest_api":
                from homeassistant.helpers.network import get_url
                token = self.entry.data[CONF_API_KEY]
                base = get_url(self.hass, prefer_external=False)
                method = inp.get("method", "GET").upper()
                url = base + inp["path"]
                session = async_get_clientsession(self.hass)
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                if method == "GET":
                    async with session.get(url, headers=headers) as r:
                        return (await r.text())[:3000]
                elif method == "POST":
                    async with session.post(url, headers=headers, json=inp.get("body", {})) as r:
                        return (await r.text())[:3000]
                elif method == "DELETE":
                    async with session.delete(url, headers=headers) as r:
                        return (await r.text())[:1000]

        except Exception as err:
            _LOGGER.error("Tool %s error: %s", tool_name, err)
            return f"Błąd: {err}"

        return "Nieznane narzędzie"

    def _get_states_summary(self) -> str:
        lines = []
        for state in self.hass.states.async_all():
            if state.domain not in ("light", "switch", "climate", "sensor",
                                    "binary_sensor", "cover", "automation",
                                    "script", "input_boolean"):
                continue
            if state.state in ("unavailable", "unknown"):
                continue
            name = state.attributes.get("friendly_name", state.entity_id)
            lines.append(f"{name} [{state.entity_id}]: {state.state}")
            if len(lines) >= 100:
                break
        return "\n".join(lines)
