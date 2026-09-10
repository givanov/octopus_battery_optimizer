"""Unit tests for the dry-run persistence logic in the controller.

Verifies that the dry-run flag (a runtime toggle owned by the switch entity
and stored in entry.data) is read with the correct precedence, so a stale
copy left in entry.options by an older version cannot override the switch.

Run with:  python3 -m unittest discover -s tests -v
(No real Home Assistant required - minimal stubs are injected.)
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "octopus_battery"


def _install_ha_stubs() -> None:
    """Inject minimal homeassistant.* stubs so controller.py can import."""
    if "homeassistant" in sys.modules:
        return

    def mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    ha = mod("homeassistant")

    const = mod("homeassistant.const")
    const.ATTR_ENTITY_ID = "entity_id"
    const.STATE_ON = "on"
    const.STATE_OFF = "off"
    const.STATE_UNAVAILABLE = "unavailable"
    const.PERCENTAGE = "%"
    class EntityCategory:
        DIAGNOSTIC = "diagnostic"
        CONFIG = "config"
        NONE = "none"
    const.EntityCategory = EntityCategory

    entries = mod("homeassistant.config_entries")
    class ConfigEntry:  # minimal placeholder for the type hint
        pass
    entries.ConfigEntry = ConfigEntry

    core = mod("homeassistant.core")
    class HomeAssistant:  # minimal placeholder
        pass
    core.HomeAssistant = HomeAssistant
    def callback(func):  # HA's @callback marker
        return func
    core.callback = callback

    helpers = mod("homeassistant.helpers")
    event = mod("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *a, **k: None
    update_coordinator = mod("homeassistant.helpers.update_coordinator")
    class DataUpdateCoordinator:
        def __init__(self, *a, **k):
            pass
        @classmethod
        def __class_getitem__(cls, item):
            return cls
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    class UpdateFailed(Exception):
        pass
    update_coordinator.UpdateFailed = UpdateFailed

    # Base Entity: state defaults to STATE_UNKNOWN (this is the bug we guard against)
    entity_mod = mod("homeassistant.helpers.entity")
    class Entity:
        hass = None
        _attr_state = "unknown"
        def __init__(self):
            pass
        @property
        def state(self):
            return self._attr_state
        def async_write_ha_state(self):
            pass
        def async_will_remove_from_hass(self):
            pass
    entity_mod.Entity = Entity

    # SwitchEntity: mirrors HA's ToggleEntity @final state -> on/off mapping
    components = mod("homeassistant.components")
    sensor_comp = mod("homeassistant.components.sensor")
    class SensorStateClass:
        MEASUREMENT = "measurement"
    sensor_comp.SensorStateClass = SensorStateClass
    switch_mod = mod("homeassistant.components.switch")
    class SwitchEntity(Entity):
        _attr_is_on = None
        @property
        def state(self):
            if (is_on := self.is_on) is None:
                return None
            return "on" if is_on else "off"
        @property
        def is_on(self):
            return self._attr_is_on
        async def async_turn_on(self, **kwargs):
            raise NotImplementedError
        async def async_turn_off(self, **kwargs):
            raise NotImplementedError
    switch_mod.SwitchEntity = SwitchEntity

    # device_registry.DeviceInfo + entity_platform.AddEntitiesCallback
    device_registry = mod("homeassistant.helpers.device_registry")
    class DeviceInfo(dict):
        pass
    device_registry.DeviceInfo = DeviceInfo
    entity_platform = mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    util = mod("homeassistant.util")
    dt = mod("homeassistant.util.dt")
    dt.now = lambda tz=None: None

    ha.const = const
    ha.config_entries = entries
    ha.core = core
    ha.helpers = helpers
    ha.util = util
    ha.components = components

    # coordinator.py imports aiohttp at module level
    aiohttp = mod("aiohttp")
    class ClientSession:
        def __init__(self, *a, **k):
            pass
    aiohttp.ClientSession = ClientSession
    class ClientResponseError(Exception):
        pass
    aiohttp.ClientResponseError = ClientResponseError
    class ClientError(Exception):
        pass
    aiohttp.ClientError = ClientError


_install_ha_stubs()

# Import const.py + controller.py without executing the package __init__.py.
if "octopus_battery" not in sys.modules:
    _pkg = types.ModuleType("octopus_battery")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["octopus_battery"] = _pkg

from octopus_battery.const import CONF_DRY_RUN  # noqa: E402
from octopus_battery.controller import BatteryController  # noqa: E402


def _entry(data: dict, options: dict) -> MagicMock:
    e = MagicMock()
    e.data = data
    e.options = options
    e.entry_id = "test-entry"
    return e


class TestReadDryRun(unittest.TestCase):
    """The dry-run flag must prefer the switch-owned entry.data value."""

    def setUp(self) -> None:
        # _read_dry_run is a pure reader; call it unbound with a dummy self.
        self.read = BatteryController._read_dry_run
        self.dummy = object()

    def test_data_wins_over_stale_options(self) -> None:
        # The bug scenario: switch set dry_run=True in entry.data, but an old
        # options-flow copy still says False. entry.data must win.
        entry = _entry(
            data={CONF_DRY_RUN: True},
            options={CONF_DRY_RUN: False},
        )
        self.assertIs(self.read(self.dummy, entry), True)

    def test_data_false_wins_over_options_true(self) -> None:
        entry = _entry(
            data={CONF_DRY_RUN: False},
            options={CONF_DRY_RUN: True},
        )
        self.assertIs(self.read(self.dummy, entry), False)

    def test_falls_back_to_options(self) -> None:
        # Legacy entry: only options has the key.
        entry = _entry(data={}, options={CONF_DRY_RUN: True})
        self.assertIs(self.read(self.dummy, entry), True)

    def test_falls_back_to_legacy_read_only(self) -> None:
        entry = _entry(data={"read_only": True}, options={})
        self.assertIs(self.read(self.dummy, entry), True)

    def test_defaults_to_false(self) -> None:
        entry = _entry(data={}, options={})
        self.assertIs(self.read(self.dummy, entry), False)

    def test_coerces_truthy_strings(self) -> None:
        # vol.Coerce(bool) yields real bools, but be defensive about truthiness.
        entry = _entry(data={CONF_DRY_RUN: "1"}, options={})
        self.assertIs(self.read(self.dummy, entry), True)


class TestSwitchState(unittest.TestCase):
    """The Dry run switch must resolve to on/off, never 'unknown'.

    Regression test: DryRunSwitch previously subclassed the base Entity,
    whose state property returns STATE_UNKNOWN. It must subclass
    SwitchEntity so is_on maps to an on/off state.
    """

    def _make_switch(self, dry_run) -> MagicMock:
        from octopus_battery.switch import DryRunSwitch

        controller = MagicMock()
        controller.dry_run = dry_run
        controller.add_listener = MagicMock()
        controller.remove_listener = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.title = "Test"
        return DryRunSwitch(entry, controller)

    def test_switch_is_a_switch_entity(self) -> None:
        from homeassistant.components.switch import SwitchEntity
        from octopus_battery.switch import DryRunSwitch

        self.assertTrue(issubclass(DryRunSwitch, SwitchEntity))

    def test_state_on_when_dry_run(self) -> None:
        sw = self._make_switch(True)
        self.assertEqual(sw.state, "on")

    def test_state_off_when_not_dry_run(self) -> None:
        sw = self._make_switch(False)
        self.assertEqual(sw.state, "off")

    def test_state_tracks_controller(self) -> None:
        sw = self._make_switch(False)
        self.assertEqual(sw.state, "off")
        # Simulate the controller flipping the flag; state must follow.
        sw._controller.dry_run = True
        self.assertEqual(sw.state, "on")


class TestEntityRemoval(unittest.TestCase):
    """async_will_remove_from_hass must be awaitable.

    Regression test: Home Assistant calls ``await entity.async_will_remove_from_hass()``
    during entity removal. If the override is a sync @callback method it returns
    None, so ``await None`` raises ``TypeError: 'NoneType' object can't be
    awaited`` and the entity is never removed (causing 'unique ID already
    exists' errors on reload). Both the sensor and switch must override it as a
    coroutine.
    """

    def test_sensor_hook_is_coroutine(self) -> None:
        import inspect
        from octopus_battery.sensor import BaseBatterySensor

        self.assertTrue(
            inspect.iscoroutinefunction(BaseBatterySensor.async_will_remove_from_hass),
            "BaseBatterySensor.async_will_remove_from_hass must be a coroutine",
        )

    def test_switch_hook_is_coroutine(self) -> None:
        import inspect
        from octopus_battery.switch import DryRunSwitch

        self.assertTrue(
            inspect.iscoroutinefunction(DryRunSwitch.async_will_remove_from_hass),
            "DryRunSwitch.async_will_remove_from_hass must be a coroutine",
        )

    def test_switch_hook_removes_listener_when_awaited(self) -> None:
        import asyncio
        from octopus_battery.switch import DryRunSwitch

        controller = MagicMock()
        controller.add_listener = MagicMock()
        controller.remove_listener = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test-entry"
        entry.title = "Test"
        sw = DryRunSwitch(entry, controller)

        # The listener is registered on construction.
        controller.add_listener.assert_called_once()

        # Awaiting the hook must unregister the listener (and not raise).
        asyncio.run(sw.async_will_remove_from_hass())
        controller.remove_listener.assert_called_once_with(sw._notify)


if __name__ == "__main__":
    unittest.main()
