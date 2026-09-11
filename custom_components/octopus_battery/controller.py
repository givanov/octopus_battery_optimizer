"""Battery controller state machine.

Evaluates the current mode on a timer (and whenever prices refresh) and
drives the two physical switches:

* ``battery_switch`` - smart plug feeding the battery charger
* ``shelly_switch``  - contactor selecting mains (ON) vs battery (OFF)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_SWITCH,
    CONF_CHARGE_HOURS,
    CONF_CHARGE_TARGET_SOC,
    CONF_CHECK_INTERVAL,
    CONF_DISCHARGE_STOP_SOC,
    CONF_SHELLY_SWITCH,
    CONF_SOC_SENSOR,
    CONF_TOPUP_TARGET_SOC,
    CONF_TOPUP_TRIGGER_SOC,
    CONF_USE_HOURS,
    CONF_DRY_RUN,
    DEFAULT_CHECK_INTERVAL,
    MODE_IDLE_NOT_CHARGING,
    VALID_MODES,
)
from .coordinator import OctopusPriceCoordinator
from .helpers import effective_data
from .schedule import Block, decide_mode, select_schedule, switches_for_mode

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[], None]


class BatteryController:
    """Runs the charge/discharge state machine for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: OctopusPriceCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._data = effective_data(entry)

        self.mode: str = MODE_IDLE_NOT_CHARGING
        self.use_block: Optional[Block] = None
        self.charge_block: Optional[Block] = None
        self.soc: Optional[float] = None
        self.last_evaluation: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.override: Optional[str] = None
        self.topup_active: bool = False
        # Dry-run mode: compute the schedule but don't touch the switches.
        # The dry-run flag is a runtime toggle owned by the switch entity and
        # stored in entry.data. Prefer entry.data so a stale copy left in
        # entry.options by an older version (when it was part of the options
        # flow) cannot override the switch. Fall back to the legacy
        # "read_only" key for entries created before the rename.
        self.dry_run: bool = self._read_dry_run(entry)

        self._listeners: list[Listener] = []
        self._unsub_tick: Optional[Callable[[], None]] = None
        self._unsub_prices: Optional[Callable[[], None]] = None
        self._applied: Optional[tuple[bool, bool]] = None

    def _read_dry_run(self, entry: ConfigEntry) -> bool:
        """Resolve the dry-run flag, preferring the switch-owned entry.data.

        Order: entry.data[dry_run] -> entry.options[dry_run] -> legacy
        entry.data[read_only] -> False. This keeps the switch as the single
        source of truth even for entries created by older versions where
        dry_run was also written to entry.options.
        """
        if CONF_DRY_RUN in entry.data:
            return bool(entry.data[CONF_DRY_RUN])
        if CONF_DRY_RUN in entry.options:
            return bool(entry.options[CONF_DRY_RUN])
        if "read_only" in entry.data:
            return bool(entry.data["read_only"])
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def async_start(self) -> None:
        """Perform the first evaluation and register the periodic tick."""
        await self.async_evaluate()
        interval = timedelta(minutes=self._data.get(CONF_CHECK_INTERVAL, DEFAULT_CHECK_INTERVAL))
        self._unsub_tick = async_track_time_interval(
            self._hass, self._async_tick, interval
        )
        self._unsub_prices = self._coordinator.async_add_listener(
            self._on_prices_updated
        )
        _LOGGER.info(
            "Controller started (evaluates every %s minutes)", interval.total_seconds() / 60
        )

    async def async_stop(self) -> None:
        """Cancel timers and listeners."""
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        if self._unsub_prices is not None:
            self._unsub_prices()
            self._unsub_prices = None

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - listener bugs must not kill the loop
                _LOGGER.exception("Sensor listener raised an exception")

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    async def _async_tick(self, _now: object) -> None:
        await self.async_evaluate()

    async def _on_prices_updated(self) -> None:
        """Re-evaluate immediately when new prices arrive.

        Home Assistant invokes coordinator listeners with no arguments
        (``update_callback()``), so this takes none.
        """
        await self.async_evaluate()

    def _read_soc(self) -> Optional[float]:
        state = self._hass.states.get(self._data[CONF_SOC_SENSOR])
        if state is None:
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if value < 0 or value > 100:
            _LOGGER.warning(
                "Battery SoC sensor %s reported %s outside 0-100",
                self._data[CONF_SOC_SENSOR],
                value,
            )
            return None
        return value

    async def async_evaluate(self) -> None:
        """Re-evaluate the mode and apply switch states if they changed."""
        data = self._data
        now = dt_util.now()
        self.soc = self._read_soc()
        self.last_evaluation = now
        self.last_error = None

        use_block: Optional[Block] = None
        charge_block: Optional[Block] = None

        if self._coordinator.last_update_success and self._coordinator.data:
            try:
                use_block, charge_block = select_schedule(
                    self._coordinator.data,
                    use_hours=data[CONF_USE_HOURS],
                    charge_hours=data[CONF_CHARGE_HOURS],
                    now=now,
                )
            except Exception as err:  # noqa: BLE001
                self.last_error = f"Schedule selection failed: {err}"
                _LOGGER.exception("Schedule selection failed")
        else:
            self.last_error = "No price data available yet"
            _LOGGER.debug("No price data available; using top-up logic only")

        if self.override is not None:
            mode = self.override
            _LOGGER.debug("Override active: %s", mode)
        else:
            mode, self.topup_active = decide_mode(
                now=now,
                soc=self.soc,
                use_block=use_block,
                charge_block=charge_block,
                discharge_stop_soc=data[CONF_DISCHARGE_STOP_SOC],
                topup_trigger_soc=data[CONF_TOPUP_TRIGGER_SOC],
                topup_target_soc=data[CONF_TOPUP_TARGET_SOC],
                charge_target_soc=data[CONF_CHARGE_TARGET_SOC],
                topup_active=self.topup_active,
            )

        self.use_block = use_block
        self.charge_block = charge_block
        self.mode = mode
        if self.dry_run:
            # Dry-run mode: compute the schedule and mode but do NOT touch
            # any physical switches.
            _LOGGER.debug("Dry-run mode: mode=%s (switches not changed)", mode)
        else:
            await self._apply_switches(mode)
        self._notify_listeners()

    # ------------------------------------------------------------------
    # Switch actuation
    # ------------------------------------------------------------------
    async def _apply_switches(self, mode: str) -> None:
        battery_on, shelly_on = switches_for_mode(mode)
        if self._applied == (battery_on, shelly_on):
            return

        data = self._data
        errors: list[str] = []

        if not await self._set_switch(data[CONF_BATTERY_SWITCH], battery_on):
            errors.append(f"battery switch: {data[CONF_BATTERY_SWITCH]}")
        if not await self._set_switch(data[CONF_SHELLY_SWITCH], shelly_on):
            errors.append(f"shelly switch: {data[CONF_SHELLY_SWITCH]}")

        if errors:
            self.last_error = "Failed to set: " + ", ".join(errors)
            _LOGGER.warning(
                "Mode %s requested (battery plug=%s, shelly=%s) but some "
                "switches could not be set: %s",
                mode,
                battery_on,
                shelly_on,
                self.last_error,
            )
            return

        self._applied = (battery_on, shelly_on)
        _LOGGER.info(
            "Mode: %s | battery plug: %s | shelly (mains): %s | SoC: %s",
            mode,
            "ON" if battery_on else "OFF",
            "ON" if shelly_on else "OFF",
            f"{self.soc:.1f}%" if self.soc is not None else "unknown",
        )

    async def _set_switch(self, entity_id: str, turn_on: bool) -> bool:
        state = self._hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Switch entity %s not found", entity_id)
            return False

        if state.state in (STATE_UNAVAILABLE, "unknown"):
            _LOGGER.warning("Switch %s is %s; skipping", entity_id, state.state)
            return False

        desired = STATE_ON if turn_on else "off"
        if state.state == desired:
            return True

        service = "turn_on" if turn_on else "turn_off"
        try:
            await self._hass.services.async_call(
                "homeassistant",
                service,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to call %s on %s", service, entity_id)
            return False

        # Many switches (e.g. Shelly) report their new state asynchronously,
        # so give them a moment before verifying.
        await asyncio.sleep(0.5)
        new_state = self._hass.states.get(entity_id)
        if new_state is not None and new_state.state == desired:
            return True

        # The command was sent; the switch may simply be slow to report back.
        # Warn but treat as success so a slow device doesn't wedge the state machine.
        _LOGGER.warning(
            "Switch %s did not confirm state %s (now: %s)",
            entity_id,
            desired,
            new_state.state if new_state else "missing",
        )
        return True

    # ------------------------------------------------------------------
    # Manual override
    # ------------------------------------------------------------------
    async def async_set_override(self, mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}")
        self.override = mode
        _LOGGER.info("Manual override set to: %s", mode)
        await self.async_evaluate()

    async def async_clear_override(self) -> None:
        self.override = None
        _LOGGER.info("Manual override cleared, resuming automatic control")
        await self.async_evaluate()

    # ------------------------------------------------------------------
    # Dry-run mode
    # ------------------------------------------------------------------
    def set_dry_run(self, value: bool) -> None:
        """Toggle dry-run mode.

        When enabled the integration keeps computing the schedule and the
        would-be mode, but never changes the physical switches. The previous
        ``_applied`` switch state is forgotten so that re-enabling control
        re-applies the current mode's switches.
        """
        value = bool(value)
        if value == self.dry_run:
            return
        self.dry_run = value
        self._applied = None  # force re-apply when control resumes
        _LOGGER.info("Dry-run mode %s", "enabled" if value else "disabled")
        self._notify_listeners()

    async def async_set_dry_run(self, value: bool) -> None:
        """Toggle dry-run mode and re-evaluate."""
        self.set_dry_run(value)
        await self.async_evaluate()
