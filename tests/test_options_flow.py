"""Unit tests for the options flow (reconfiguration after initial setup).

Verifies that:
  * the config flow exposes an options flow via async_get_options_flow,
  * the options form is pre-filled with the entry's current values,
  * valid input is saved as the entry's options,
  * the same cross-field validation as the user step is applied,
  * dry_run stays out of the options schema (the switch owns it),
  * the initial user flow still works.

Run with:  python3 -m unittest discover -s tests -v
(No real Home Assistant required - minimal stubs are injected.)
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import test_dry_run  # noqa: E402,F401  (shared HA stub installer)

test_dry_run._install_ha_stubs()

# Import config_flow.py without executing the package __init__.py.
if "octopus_battery" not in sys.modules:
    _pkg = types.ModuleType("octopus_battery")
    _pkg.__path__ = [str(ROOT / "custom_components" / "octopus_battery")]
    sys.modules["octopus_battery"] = _pkg

from octopus_battery import config_flow  # noqa: E402
from octopus_battery.const import (  # noqa: E402
    CONF_BATTERY_SWITCH,
    CONF_CHARGE_HOURS,
    CONF_CHARGE_TARGET_SOC,
    CONF_CHECK_INTERVAL,
    CONF_DISCHARGE_STOP_SOC,
    CONF_DRY_RUN,
    CONF_PRODUCT_CODE,
    CONF_SHELLY_SWITCH,
    CONF_SOC_SENSOR,
    CONF_TARIFF_CODE,
    CONF_TOPUP_TARGET_SOC,
    CONF_TOPUP_TRIGGER_SOC,
    CONF_USE_HOURS,
)


def _entry(data: dict, options: dict | None = None) -> MagicMock:
    e = MagicMock()
    e.data = data
    e.options = options or {}
    e.entry_id = "test-entry"
    e.title = "Test"
    return e


def _valid_input() -> dict:
    return {
        CONF_BATTERY_SWITCH: "switch.battery_plug",
        CONF_SHELLY_SWITCH: "switch.shelly",
        CONF_SOC_SENSOR: "sensor.battery_soc",
        CONF_USE_HOURS: 4,
        CONF_CHARGE_HOURS: 4,
        CONF_PRODUCT_CODE: "AGILE-24-10-01",
        CONF_TARIFF_CODE: "E-1R-AGILE-24-10-01-A",
        CONF_DISCHARGE_STOP_SOC: 5,
        CONF_TOPUP_TRIGGER_SOC: 3,
        CONF_TOPUP_TARGET_SOC: 5,
        CONF_CHARGE_TARGET_SOC: 100,
        CONF_CHECK_INTERVAL: 5,
    }


def _schema_defaults(schema: Any) -> dict:
    """Map field name -> default for a (stub) voluptuous schema."""
    return {
        getattr(key, "key", key): getattr(key, "default", None)
        for key in schema.schema
    }


class TestOptionsFlowExposed(unittest.TestCase):
    """The config flow must expose an options flow for reconfiguration."""

    def test_returns_options_flow_instance(self) -> None:
        flow = config_flow.OctopusBatteryConfigFlow()
        entry = _entry(_valid_input())
        result = flow.async_get_options_flow(entry)
        self.assertIsInstance(result, config_flow.OctopusBatteryOptionsFlow)
        # The entry must be handed to the options flow (HA calls
        # ``handler.async_get_options_flow(entry)`` and the flow reads
        # ``self.config_entry``).
        self.assertIs(result.config_entry, entry)

    def test_options_flow_is_an_ha_options_flow(self) -> None:
        from homeassistant.config_entries import OptionsFlow

        self.assertTrue(
            issubclass(config_flow.OctopusBatteryOptionsFlow, OptionsFlow)
        )


class TestOptionsStepInit(unittest.TestCase):
    """async_step_init: pre-filled form, validation, and saving."""

    def _flow(self, data: dict | None = None, options: dict | None = None):
        # Constructed the same way HA's OptionsFlowManager does:
        # handler.async_get_options_flow(entry) -> OctopusBatteryOptionsFlow(entry)
        return config_flow.OctopusBatteryOptionsFlow(
            _entry(data or _valid_input(), options)
        )

    def test_first_call_shows_prefilled_form(self) -> None:
        flow = self._flow(data={**_valid_input(), CONF_USE_HOURS: 6})
        result = asyncio.run(flow.async_step_init())
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "init")
        defaults = _schema_defaults(result["data_schema"])
        self.assertEqual(defaults[CONF_USE_HOURS], 6)
        self.assertEqual(defaults[CONF_BATTERY_SWITCH], "switch.battery_plug")
        self.assertEqual(defaults[CONF_SHELLY_SWITCH], "switch.shelly")
        self.assertEqual(defaults[CONF_SOC_SENSOR], "sensor.battery_soc")

    def test_saved_options_win_in_prefill(self) -> None:
        flow = self._flow(data=_valid_input(), options={CONF_USE_HOURS: 7})
        result = asyncio.run(flow.async_step_init())
        defaults = _schema_defaults(result["data_schema"])
        self.assertEqual(defaults[CONF_USE_HOURS], 7)

    def test_dry_run_not_in_options_schema(self) -> None:
        # The Dry run switch is the single source of truth for dry_run.
        flow = self._flow()
        result = asyncio.run(flow.async_step_init())
        self.assertNotIn(CONF_DRY_RUN, _schema_defaults(result["data_schema"]))

    def test_valid_input_saved_as_options(self) -> None:
        flow = self._flow()
        user_input = {**_valid_input(), CONF_USE_HOURS: 6}
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], user_input)

    def test_trigger_above_target_rejected(self) -> None:
        flow = self._flow()
        user_input = {
            **_valid_input(),
            CONF_TOPUP_TRIGGER_SOC: 10,
            CONF_TOPUP_TARGET_SOC: 5,
        }
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(result["type"], "form")
        self.assertEqual(
            result["errors"].get(CONF_TOPUP_TRIGGER_SOC), "trigger_above_target"
        )

    def test_stop_above_charge_target_rejected(self) -> None:
        flow = self._flow()
        user_input = {
            **_valid_input(),
            CONF_DISCHARGE_STOP_SOC: 100,
            CONF_CHARGE_TARGET_SOC: 90,
        }
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(
            result["errors"].get(CONF_DISCHARGE_STOP_SOC), "stop_above_charge_target"
        )

    def test_same_switch_rejected(self) -> None:
        flow = self._flow()
        user_input = {**_valid_input(), CONF_SHELLY_SWITCH: "switch.battery_plug"}
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(result["errors"].get(CONF_SHELLY_SWITCH), "same_as_battery")

    def test_overlapping_blocks_rejected(self) -> None:
        flow = self._flow()
        user_input = {**_valid_input(), CONF_USE_HOURS: 20, CONF_CHARGE_HOURS: 5}
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(result["errors"].get(CONF_CHARGE_HOURS), "blocks_overlap")

    def test_missing_entity_rejected(self) -> None:
        flow = self._flow()
        user_input = {**_valid_input(), CONF_SOC_SENSOR: None}
        result = asyncio.run(flow.async_step_init(user_input))
        self.assertEqual(result["errors"].get(CONF_SOC_SENSOR), "required")


class TestUserFlowStillWorks(unittest.TestCase):
    """Regression guard: the initial user flow must keep working."""

    def test_user_step_shows_form(self) -> None:
        flow = config_flow.OctopusBatteryConfigFlow()
        result = asyncio.run(flow.async_step_user())
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "user")

    def test_user_step_creates_entry(self) -> None:
        flow = config_flow.OctopusBatteryConfigFlow()
        result = asyncio.run(flow.async_step_user(_valid_input()))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"], _valid_input())


if __name__ == "__main__":
    unittest.main()
