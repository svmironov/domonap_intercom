"""Diagnostics deliberately contain no credentials, addresses or event payloads."""
from .const import DOMAIN
from .runtime_status import runtime_snapshot


async def async_get_config_entry_diagnostics(hass, entry):
    snapshot = runtime_snapshot(hass.data.get(DOMAIN, {}).get(entry.entry_id, {}))
    return {key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in snapshot.items()}
