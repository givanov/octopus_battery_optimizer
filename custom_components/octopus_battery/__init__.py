"""The Octopus Battery Optimizer integration.

Switches a battery between charging (on the cheapest hours), discharging
(on the most expensive hours) and a maintenance top-up, using hourly
Octopus Agile prices.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    ATTR_ENTRY_ID,
    ATTR_MODE,
    DOMAIN,
    PLATFORMS,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_SET_MODE,
    VALID_MODES,
)
from .controller import BatteryController
from .coordinator import OctopusPriceCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MODE): vol.In(list(VALID_MODES)),
        vol.Optional(ATTR_ENTRY_ID): vol.Coerce(str),
    }
)
SERVICE_CLEAR_OVERRIDE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTRY_ID): vol.Coerce(str)}
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Domain-level setup: register the services once."""
    hass.data.setdefault(DOMAIN, {})
    hass.services.async_register(
        DOMAIN, SERVICE_SET_MODE, _async_set_mode, schema=SERVICE_SET_MODE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OVERRIDE,
        _async_clear_override,
        schema=SERVICE_CLEAR_OVERRIDE_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Octopus Battery Optimizer from a config entry."""
    if entry.entry_id in hass.data.get(DOMAIN, {}):
        # Guard against a double setup (e.g. a reload racing with the initial
        # setup), which would register the same entities twice and trigger
        # "unique ID already exists" errors.
        _LOGGER.warning(
            "Entry %s is already set up; skipping duplicate setup", entry.entry_id
        )
        return True

    coordinator = OctopusPriceCoordinator(hass, entry)

    try:
        await coordinator.async_refresh()
    except UpdateFailed as err:
        raise ConfigEntryNotReady(f"Cannot fetch Octopus prices: {err}") from err

    controller = BatteryController(hass, entry, coordinator)
    await controller.async_start()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "controller": controller,
    }

    # Apply option changes (entity ids, hours, thresholds, ...) by
    # reloading the entry, which re-creates coordinator + controller.
    entry.async_on_unload(entry.add_update_listener(_async_update_entry))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        controller: BatteryController = hass.data[DOMAIN][entry.entry_id]["controller"]
        await controller.async_stop()
        del hass.data[DOMAIN][entry.entry_id]
    return unload_ok


async def _async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle an options update by reloading the entry."""
    _LOGGER.info("Options updated - reloading %s entry", DOMAIN)
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def _target_controllers(
    hass: HomeAssistant, entry_id: str | None
) -> list[BatteryController]:
    """Resolve the controller(s) a service call should act on."""
    entries = hass.data.get(DOMAIN, {})
    if entry_id is not None:
        if entry_id not in entries:
            raise ServiceValidationError(
                reason=f"No {DOMAIN} entry with id {entry_id}"
            )
        return [entries[entry_id]["controller"]]
    controllers = [data["controller"] for data in entries.values()]
    if not controllers:
        raise ServiceValidationError(
            reason=f"No {DOMAIN} entry is configured"
        )
    return controllers


async def _async_set_mode(call: ServiceCall) -> None:
    """Force the controller into a specific mode until cleared."""
    mode = call.data[ATTR_MODE]
    for controller in _target_controllers(call.hass, call.data.get(ATTR_ENTRY_ID)):
        await controller.async_set_override(mode)


async def _async_clear_override(call: ServiceCall) -> None:
    """Clear a manual override and resume automatic control."""
    for controller in _target_controllers(call.hass, call.data.get(ATTR_ENTRY_ID)):
        await controller.async_clear_override()
