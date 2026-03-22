"""Claude Conversation platform for Home Assistant."""
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .entity import ClaudeConversationEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    agent = ClaudeConversationEntity(hass, entry)
    async_add_entities([agent])
