# Claude HA Bridge — Home Assistant Add-on

[![Open your Home Assistant instance and show the add add-on repository dialog.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https://github.com/plubnicki/ha-claude-bridge)

## Instalacja

1. Kliknij przycisk powyżej LUB w HA: **Ustawienia → Dodatki → Sklep → ⋮ → Repozytoria**
2. Dodaj: `https://github.com/plubnicki/ha-claude-bridge`
3. Znajdź **"Claude HA Bridge"** i zainstaluj
4. W zakładce **Konfiguracja** wpisz `anthropic_api_key`
5. Uruchom add-on

## Konfiguracja

| Opcja | Opis |
|-------|------|
| `anthropic_api_key` | Klucz API z console.anthropic.com |
| `ha_bridge_token` | Token zabezpieczający endpoint (dowolny string) |
| `claude_model` | Model Claude (domyślnie claude-sonnet-4-20250514) |
| `max_tokens` | Maks. tokenów odpowiedzi (domyślnie 1024) |
| `system_prompt` | Osobowość asystenta |

## Endpointy

- `POST http://homeassistant.local:8765/query` — rozmowa z historią
- `POST http://homeassistant.local:8765/query/simple` — jednorazowe pytanie
- `GET  http://homeassistant.local:8765/health` — status
