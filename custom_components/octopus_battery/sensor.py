"""Sensor platform for the Octopus Battery Optimizer integration."""

from __future__ import annotations

from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controller import BatteryController

PARITY = "p/kWh"


def _fmt_time(value: Optional[Any]) -> Optional[str]:
    """Format a datetime as HH:MM for sensor states."""
    if value is None:
        return None
    return value.strftime("%H:%M")


class BaseBatterySensor(Entity):
    """Base class: no polling, updates are pushed by the controller."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        entry: ConfigEntry,
        controller: BatteryController,
        translation_key: str,
        name: str,
        device_class: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._attr_translation_key = translation_key
        # Explicit name so sensors are labelled correctly even if the
        # translation file is not loaded; the translation (if present)
        # takes precedence.
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}-{translation_key}"
        if device_class is not None:
            self._attr_device_class = device_class
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="community",
            model="octopus battery optimizer",
        )
        controller.add_listener(self._notify)

    @callback
    def _notify(self) -> None:
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        # Must be a coroutine: Home Assistant awaits this hook during entity
        # removal. A sync @callback version returns None, so `await None`
        # raises TypeError and the entity is never actually removed.
        self._controller.remove_listener(self._notify)


class ModeSensor(BaseBatterySensor):
    """Current controller mode."""

    def __init__(self, entry: ConfigEntry, controller: BatteryController) -> None:
        super().__init__(entry, controller, "mode", "Mode")

    @property
    def state(self) -> Optional[str]:
        return self._controller.mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "dry_run": self._controller.dry_run,
            "override_active": self._controller.override is not None,
            "top_up_in_progress": self._controller.topup_active,
            "last_error": self._controller.last_error,
        }


class BlockTimeSensor(BaseBatterySensor):
    """Start or end time of a selected price block (HH:MM)."""

    def __init__(
        self,
        entry: ConfigEntry,
        controller: BatteryController,
        translation_key: str,
        name: str,
        block_attr: str,
        which: str,
    ) -> None:
        super().__init__(entry, controller, translation_key, name)
        self._block_attr = block_attr
        self._which = which  # "start" | "end"

    @property
    def state(self) -> Optional[str]:
        block = getattr(self._controller, self._block_attr)
        if block is None:
            return None
        return _fmt_time(getattr(block, self._which))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        block = getattr(self._controller, self._block_attr)
        if block is None:
            return {}
        return {
            "start": block.start.isoformat(),
            "end": block.end.isoformat(),
            "hours": block.hours,
            "total_price": round(block.total, 2),
        }


class BlockPriceSensor(BaseBatterySensor):
    """Total price (p/kWh) of a selected block."""

    _attr_native_unit_of_measurement = PARITY

    def __init__(
        self,
        entry: ConfigEntry,
        controller: BatteryController,
        translation_key: str,
        name: str,
        block_attr: str,
    ) -> None:
        super().__init__(entry, controller, translation_key, name)
        self._block_attr = block_attr

    @property
    def state(self) -> Optional[float]:
        block = getattr(self._controller, self._block_attr)
        if block is None:
            return None
        return round(block.total, 2)


# Battery level state thresholds, as a percentage of state-of-charge.
SOC_CRITICAL_BELOW = 10.0  # strictly below this -> "critical"
SOC_LOW_BELOW = 20.0  # below this (and not critical) -> "low"
SOC_FULL_AT = 100.0  # at this -> "fully_charged"

BATTERY_STATE_CRITICAL = "critical"
BATTERY_STATE_LOW = "low"
BATTERY_STATE_OK = "ok"
BATTERY_STATE_FULLY_CHARGED = "fully_charged"


def battery_level_state(soc: Optional[float]) -> Optional[str]:
    """Map a battery state-of-charge (%) to a human-readable level state.

    * ``critical``       strictly below 10 %
    * ``low``            10 % (inclusive) up to 20 %
    * ``fully_charged``  at 100 %
    * ``ok``             20 % (inclusive) up to (but not at) 100 %
    * ``None``           when the SoC is unknown
    """
    if soc is None:
        return None
    if soc < SOC_CRITICAL_BELOW:
        return BATTERY_STATE_CRITICAL
    if soc < SOC_LOW_BELOW:
        return BATTERY_STATE_LOW
    if soc >= SOC_FULL_AT:
        return BATTERY_STATE_FULLY_CHARGED
    return BATTERY_STATE_OK


class BatteryLevelSensor(BaseBatterySensor):
    """Battery level as a state: critical / low / ok / fully_charged."""

    def __init__(self, entry: ConfigEntry, controller: BatteryController) -> None:
        super().__init__(entry, controller, "battery_level", "Battery level")

    @property
    def state(self) -> Optional[str]:
        return battery_level_state(self._controller.soc)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Keep the exact SoC (%) available as an attribute for dashboards.
        soc = self._controller.soc
        return {"soc": None if soc is None else round(soc, 1)}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor entities."""
    controller: BatteryController = hass.data[DOMAIN][entry.entry_id]["controller"]

    async_add_entities(
        [
            ModeSensor(entry, controller),
            BlockTimeSensor(
                entry, controller, "use_block_start", "Use block start",
                "use_block", "start",
            ),
            BlockTimeSensor(
                entry, controller, "use_block_end", "Use block end",
                "use_block", "end",
            ),
            BlockPriceSensor(
                entry, controller, "use_block_price", "Use block price", "use_block"
            ),
            BlockTimeSensor(
                entry, controller, "charge_block_start", "Charge block start",
                "charge_block", "start",
            ),
            BlockTimeSensor(
                entry, controller, "charge_block_end", "Charge block end",
                "charge_block", "end",
            ),
            BlockPriceSensor(
                entry, controller, "charge_block_price", "Charge block price",
                "charge_block",
            ),
            BatteryLevelSensor(entry, controller),
        ]
    )
