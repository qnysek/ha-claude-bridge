#!/usr/bin/with-contenv bashio

export ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')
export HA_BRIDGE_TOKEN=$(bashio::config 'ha_bridge_token')
export CLAUDE_MODEL=$(bashio::config 'claude_model')
export MAX_TOKENS=$(bashio::config 'max_tokens')
export SYSTEM_PROMPT=$(bashio::config 'system_prompt')
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Starting Claude HA Bridge on port 8765..."
exec uvicorn server:app --host 0.0.0.0 --port 8765
