from __future__ import annotations

import logging
import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er

from .const import AUTH_MODE_PANEL, DOMAIN, PARAM_AUTH_MODE

_LOGGER = logging.getLogger(__name__)


def extract_phone_digits(entry: ConfigEntry) -> str | None:
    """Return phone number containing only digits.

    Prefers `entry.data["phone_number"]` (already sanitized by config flow),
    falls back to parsing `entry.title` (format like "+7 9991234567").
    """

    # Prefer explicit data (from config_flow)
    phone = entry.data.get("phone_number")
    if isinstance(phone, str) and phone.strip():
        digits = re.sub(r"\D", "", phone)
        return digits or None

    # Fallback: parse from title
    title = entry.title or ""
    digits = re.sub(r"\D", "", title)
    return digits or None


def panel_entity_prefix(entry: ConfigEntry) -> str:
    """Return the entity-registry namespace used by a Panel config entry."""
    if entry.data.get(PARAM_AUTH_MODE) != AUTH_MODE_PANEL:
        return ""
    return f"panel:{entry.entry_id}:"


def scoped_entity_unique_id(entry: ConfigEntry, raw_unique_id: str) -> str:
    """Scope Panel entity unique IDs without changing legacy phone entries."""
    prefix = panel_entity_prefix(entry)
    return f"{prefix}{raw_unique_id}" if prefix else raw_unique_id


def event_belongs_to_entry(
    event_data: dict,
    entry_id: str,
    *,
    panel_scoped: bool,
) -> bool:
    """Return whether a global Domonap event belongs to this config entry.

    Panel runtime events are tagged with ``config_entry_id`` and must never leak
    into entities owned by another Panel account. Legacy phone/SMS events predate
    that field and therefore accept only untagged events; this also prevents a
    Panel event from accidentally updating a legacy phone entity that happens to
    expose the same DoorId.
    """
    source_entry_id = event_data.get("config_entry_id")
    if source_entry_id is not None:
        return source_entry_id == entry_id
    return not panel_scoped


def migrate_panel_entity_unique_ids(hass, entry: ConfigEntry) -> None:
    """Prefix existing Panel registry unique IDs while preserving entity_id.

    Home Assistant entity uniqueness is global per platform, not per config
    entry. Two Rubetek accounts can expose the same DoorId/KeyId/camera id, so
    unscoped historical IDs collide. Updating the registry before platforms are
    set up preserves existing entity_id values and allows the second account to
    create its own entities.
    """
    prefix = panel_entity_prefix(entry)
    if not prefix:
        return

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.platform != DOMAIN or entity.unique_id.startswith(prefix):
            continue
        new_unique_id = f"{prefix}{entity.unique_id}"
        try:
            registry.async_update_entity(
                entity.entity_id,
                new_unique_id=new_unique_id,
            )
        except ValueError:
            _LOGGER.warning(
                "Cannot migrate Domonap entity %s unique_id %s -> %s; target already exists",
                entity.entity_id,
                entity.unique_id,
                new_unique_id,
            )


def find_last_call_sensor_entity_id(hass, entry_id: str) -> str | None:
    """Resolve a renamed sensor only within the selected account's registry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    raw = f"{extract_phone_digits(entry) or entry_id}_last_call_door_id"
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, scoped_entity_unique_id(entry, raw))
    entity = registry.async_get(entity_id) if entity_id else None
    if entity is not None and entity.config_entry_id == entry_id:
        return entity_id
    return None


def scoped_device_id(entry_id: str | None, raw_id: str) -> str:
    """Keep device identity separate even when accounts share the same door."""
    return f"entry:{entry_id}:{raw_id}" if entry_id else str(raw_id)


def migrate_device_identifiers(hass, entry: ConfigEntry) -> None:
    """Preserve sole-owner devices; split historically shared devices per account."""
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    prefix = f"entry:{entry.entry_id}:"
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        legacy = {(domain, value) for domain, value in device.identifiers
                  if domain == DOMAIN and not value.startswith("entry:")}
        if not legacy:
            continue
        scoped = {(DOMAIN, prefix + value) for _, value in legacy}
        if device.config_entries == {entry.entry_id}:
            devices.async_update_device(device.id, new_identifiers=(device.identifiers - legacy) | scoped)
            continue
        replacement = devices.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers=scoped,
            name=device.name, manufacturer=device.manufacturer, model=device.model,
        )
        devices.async_update_device(
            replacement.id, area_id=device.area_id, name_by_user=device.name_by_user,
            labels=device.labels, disabled_by=device.disabled_by,
        )
        for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
            if entity.device_id == device.id and entity.platform == DOMAIN:
                entities.async_update_entity(entity.entity_id, device_id=replacement.id)
        devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
