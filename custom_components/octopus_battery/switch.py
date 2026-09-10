"""Switch platform for the Octopus Battery Optimizer integration.

Exposes a single **Dry run mode** switch. When ON, the integration keeps
computing the charge/use schedule and the would-be mode, but does not change
any of the physical switches.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DRY_RUN, DOMAIN
from .controller import BatteryController


class DryRunSwitch(SwitchEntity):
    """Toggle whether the integration is allowed to drive the switches."""

    _attr_name = "Dry run mode"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, controller: BatteryController
    ) -> None:
        super().__init__()
        self._entry = entry
        self._controller = controller
        self._attr_unique_id = f"{entry.entry_id}-read_only"  # kept stable across the rename to avoid orphaning existing entities
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="community",
            model="octopus battery optimizer",
        )
        self._attr_assumed_state = True
        controller.add_listener(self._notify)

    @callback
    def _notify(self) -> None:
        self.async_write_ha_state()

    @callback
    def async_will_remove_from_hass(self) -> None:
        self._controller.remove_listener(self._notify)

    @property
    def is_on(self) -> bool:
        return self._controller.dry_run

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "mode": self._controller.mode,
            "note": (
                "When ON the schedule is still computed but the battery and "
                "Shelly switches are left untouched."
            ),
        }

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        hass = self.hass
        entry = self._entry
        # Apply immediately (no reload) so the change is responsive.
        await self._controller.async_set_dry_run(value)
        # Persist so the choice survives a restart.
        if hass is not None and entry is not None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DRY_RUN: value}
            )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the dry-run switch."""
    controller: BatteryController = hass.data[DOMAIN][entry.entry_id]["controller"]
    async_add_entities([DryRunSwitch(entry, controller)])
