"""Unit tests for the optional Home Assistant price source.

Covers the ``HomeAssistantRatesCoordinator`` (reading the BottlecapDave
``octopus_energy`` integration's day-rates event entities instead of polling
the Octopus API) and the wiring that selects it:

* the rate dicts are converted from GBP/kWh (``value_inc_vat``) to pence/kWh;
* the next/previous day entities are derived from the configured entity id;
* live rate events are merged (and other meters are filtered out);
* the combined curve is windowed the same way the API-based source is;
* ``__init__.py`` picks the right coordinator for each ``price_source``;
* the config flow requires a price entity when the source is ``homeassistant``.

Run with:  python3 -m unittest discover -s tests -v
(No real Home Assistant required - the shared minimal stubs are reused.)
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

import test_dry_run  # noqa: F401  (installs the HA stubs + package path)

from homeassistant.core import Event
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from octopus_battery import config_flow
from octopus_battery.const import (
    CONF_CHARGE_HOURS,
    CONF_PRICE_ENTITY,
    CONF_PRICE_SOURCE,
    CONF_PRODUCT_CODE,
    CONF_TARIFF_CODE,
    CONF_USE_HOURS,
    EVENT_ELECTRICITY_CURRENT_DAY_RATES,
    EVENT_ELECTRICITY_DAY_RATES,
    EVENT_ELECTRICITY_NEXT_DAY_RATES,
    EVENT_ELECTRICITY_PREVIOUS_DAY_RATES,
    PRICE_SOURCE_API,
    PRICE_SOURCE_HOMEASSISTANT,
)
from octopus_battery.coordinator import OctopusPriceCoordinator
from octopus_battery.homeassistant_rates import (
    HomeAssistantRatesCoordinator,
    _parse_datetime,
)

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeState:
    def __init__(self, attributes: dict) -> None:
        self.attributes = attributes


class _FakeBus:
    def __init__(self) -> None:
        # Mirrors HA's EventBus: one listener registration per event type.
        self.handlers: dict[str, list] = {}

    def async_listen(self, event_type, handler, *args, **kwargs) -> object:
        self.handlers.setdefault(event_type, []).append(handler)
        return lambda: self.handlers.get(event_type, []).remove(
            handler
        ) if handler in self.handlers.get(event_type, []) else None


class _FakeHass:
    def __init__(self) -> None:
        self.states: dict = {}
        self.bus = _FakeBus()
        self.tasks: list = []

    def async_create_task(self, coro, name=None) -> None:
        self.tasks.append((name, coro))
        if hasattr(coro, "close"):
            coro.close()  # avoid "coroutine was never awaited" warnings
        return None


class _FakeEntry:
    def __init__(self, data: dict, options: dict | None = None) -> None:
        self.data = data
        self.options = options or {}
        self.entry_id = "test-entry"
        self._unloads: list = []

    def async_on_unload(self, callback) -> None:
        self._unloads.append(callback)


def _coordinator(entity: str, now: datetime | None = NOW, **hours) -> HomeAssistantRatesCoordinator:
    if now is not None:
        dt_util._now_holder["now"] = now
    data = {
        CONF_PRICE_SOURCE: PRICE_SOURCE_HOMEASSISTANT,
        CONF_PRICE_ENTITY: entity,
        CONF_USE_HOURS: hours.get("use_hours", 4),
        CONF_CHARGE_HOURS: hours.get("charge_hours", 4),
    }
    hass = _FakeHass()
    entry = _FakeEntry(data)
    coord = HomeAssistantRatesCoordinator(hass, entry)
    coord.hass = hass  # the stub parent sets this too, but be explicit
    return coord


def _rate(start, end, value_inc_vat, start_iso: bool = False) -> dict:
    rate = {
        "start": start.isoformat() if start_iso else start,
        "end": end.isoformat() if start_iso else end,
        "value_inc_vat": value_inc_vat,
    }
    return rate


# ---------------------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------------------
class ParseDatetimeTests(unittest.TestCase):
    def test_datetime_passthrough(self) -> None:
        d = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        self.assertIs(_parse_datetime(d), d)

    def test_iso_string(self) -> None:
        self.assertEqual(
            _parse_datetime("2026-07-10T12:00:00+00:00"),
            datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        )

    def test_invalid_values(self) -> None:
        self.assertIsNone(_parse_datetime("not-a-date"))
        self.assertIsNone(_parse_datetime(12345))
        self.assertIsNone(_parse_datetime(None))


# ---------------------------------------------------------------------------
# GBP/kWh -> pence/kWh conversion
# ---------------------------------------------------------------------------
class ConversionTests(unittest.TestCase):
    def test_gbp_to_pence(self) -> None:
        coord = _coordinator("event.octopus_energy_123_ABC_current_day_rates")
        point = coord._to_price_point(
            _rate(
                datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
                datetime(2026, 7, 10, 13, 0, tzinfo=UTC),
                0.15,  # GBP/kWh
            )
        )
        self.assertIsNotNone(point)
        self.assertEqual(point.value, 15.0)  # scaled to pence/kWh
        self.assertEqual(point.valid_from, datetime(2026, 7, 10, 12, 0, tzinfo=UTC))
        self.assertEqual(point.valid_to, datetime(2026, 7, 10, 13, 0, tzinfo=UTC))

    def test_iso_string_rate(self) -> None:
        coord = _coordinator("event.octopus_energy_123_ABC_current_day_rates")
        point = coord._to_price_point(
            _rate(
                datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
                datetime(2026, 7, 10, 13, 0, tzinfo=UTC),
                0.05,
                start_iso=True,
            )
        )
        self.assertIsNotNone(point)
        self.assertEqual(point.value, 5.0)

    def test_invalid_rate_returns_none(self) -> None:
        coord = _coordinator("event.octopus_energy_123_ABC_current_day_rates")
        # missing end
        self.assertIsNone(
            coord._to_price_point(
                {"start": "2026-07-10T12:00:00+00:00", "value_inc_vat": 1}
            )
        )
        # unparseable start
        self.assertIsNone(
            coord._to_price_point(
                {
                    "start": "bad",
                    "end": "2026-07-10T13:00:00+00:00",
                    "value_inc_vat": 1,
                }
            )
        )
        # non-numeric value
        self.assertIsNone(
            coord._to_price_point(
                {
                    "start": "2026-07-10T12:00:00+00:00",
                    "end": "2026-07-10T13:00:00+00:00",
                    "value_inc_vat": "not-a-number",
                }
            )
        )


# ---------------------------------------------------------------------------
# Related entity ids
# ---------------------------------------------------------------------------
class RelatedEntityIdsTests(unittest.TestCase):
    def test_derives_next_and_previous(self) -> None:
        coord = _coordinator("event.octopus_energy_123_ABC_current_day_rates")
        self.assertEqual(
            coord._related_entity_ids(),
            [
                "event.octopus_energy_123_ABC_current_day_rates",
                "event.octopus_energy_123_ABC_next_day_rates",
                "event.octopus_energy_123_ABC_previous_day_rates",
            ],
        )

    def test_non_standard_suffix_uses_only_configured(self) -> None:
        coord = _coordinator("event.custom_my_rates")
        self.assertEqual(coord._related_entity_ids(), ["event.custom_my_rates"])


# ---------------------------------------------------------------------------
# Entity reading + meter learning
# ---------------------------------------------------------------------------
class ReadEntityTests(unittest.TestCase):
    def test_reads_rates_and_learns_meter(self) -> None:
        entity = "event.octopus_energy_123_ABC_current_day_rates"
        coord = _coordinator(entity)
        coord.hass.states[entity] = _FakeState(
            {
                "mpan": "ABC",
                "serial_number": "123",
                "rates": [
                    _rate(
                        datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
                        datetime(2026, 7, 10, 1, 0, tzinfo=UTC),
                        0.10,
                    )
                ],
            }
        )
        got = coord._read_entity(entity)
        self.assertEqual(len(got), 1)
        self.assertEqual(coord._mpan, "ABC")
        self.assertEqual(coord._serial, "123")

    def test_missing_entity_returns_empty(self) -> None:
        coord = _coordinator("event.x")
        self.assertEqual(coord._read_entity("event.does_not_exist"), [])


# ---------------------------------------------------------------------------
# Full update: combine current+next day, window, in pence
# ---------------------------------------------------------------------------
class UpdateDataTests(unittest.TestCase):
    def test_combines_and_windows_current_and_next_day(self) -> None:
        entity = "event.octopus_energy_123_ABC_current_day_rates"
        next_entity = entity.replace("_current_day_rates", "_next_day_rates")
        coord = _coordinator(entity, use_hours=4, charge_hours=4)
        coord.hass.states[entity] = _FakeState(
            {
                "mpan": "ABC",
                "serial_number": "123",
                "rates": [
                    _rate(
                        datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
                        datetime(2026, 7, 10, 1, 0, tzinfo=UTC),
                        0.10,
                    ),
                    _rate(
                        datetime(2026, 7, 10, 1, 0, tzinfo=UTC),
                        datetime(2026, 7, 10, 2, 0, tzinfo=UTC),
                        0.20,
                    ),
                ],
            }
        )
        coord.hass.states[next_entity] = _FakeState(
            {
                "rates": [
                    _rate(
                        datetime(2026, 7, 11, 0, 0, tzinfo=UTC),
                        datetime(2026, 7, 11, 1, 0, tzinfo=UTC),
                        0.05,
                    ),
                    _rate(
                        datetime(2026, 7, 11, 1, 0, tzinfo=UTC),
                        datetime(2026, 7, 11, 2, 0, tzinfo=UTC),
                        0.06,
                    ),
                ]
            }
        )

        data = asyncio.run(coord._async_update_data())
        values = {p.valid_from: p.value for p in data}

        # Values are in pence/kWh (GBP * 100).
        self.assertEqual(values[datetime(2026, 7, 10, 0, 0, tzinfo=UTC)], 10.0)
        self.assertEqual(values[datetime(2026, 7, 10, 1, 0, tzinfo=UTC)], 20.0)
        # The next-day early slot is included so day-anchored blocks that run
        # past midnight have a price to schedule against.
        self.assertEqual(values[datetime(2026, 7, 11, 0, 0, tzinfo=UTC)], 5.0)
        # All four slots fall inside the window; nothing else is present.
        self.assertEqual(len(data), 4)
        # Sorted ascending by start.
        starts = [p.valid_from for p in data]
        self.assertEqual(starts, sorted(starts))

    def test_no_rate_data_raises_update_failed(self) -> None:
        coord = _coordinator("event.missing_entity")
        with self.assertRaises(UpdateFailed):
            asyncio.run(coord._async_update_data())

    def test_missing_price_entity_raises_update_failed(self) -> None:
        # A homeassistant entry with no configured entity cannot read prices.
        coord = _coordinator("")
        with self.assertRaises(UpdateFailed):
            asyncio.run(coord._async_update_data())


# ---------------------------------------------------------------------------
# Live event handling (merge + other-meter filter)
# ---------------------------------------------------------------------------
class RateEventTests(unittest.TestCase):
    def test_same_meter_merged_other_meter_filtered(self) -> None:
        coord = _coordinator("event.x")
        coord._mpan = "ABC"  # target meter already known

        same_start = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        other_start = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)

        coord._handle_rate_event(
            Event(
                EVENT_ELECTRICITY_CURRENT_DAY_RATES,
                {
                    "mpan": "ABC",
                    "rates": [
                        _rate(same_start, datetime(2026, 7, 10, 13, 0, tzinfo=UTC), 0.15)
                    ],
                },
            )
        )
        self.assertIn(same_start, coord._rates)
        self.assertEqual(coord._rates[same_start].value, 15.0)

        # A different meter's rates must be ignored.
        coord._handle_rate_event(
            Event(
                EVENT_ELECTRICITY_NEXT_DAY_RATES,
                {
                    "mpan": "ZZZ",
                    "rates": [
                        _rate(
                            other_start,
                            datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
                            0.99,
                        )
                    ],
                },
            )
        )
        self.assertNotIn(other_start, coord._rates)
        # A task was queued to re-publish after the merge.
        self.assertTrue(coord.hass.tasks)

    def test_event_without_rates_is_ignored(self) -> None:
        coord = _coordinator("event.x")
        coord._mpan = "ABC"
        coord._handle_rate_event(
            Event(EVENT_ELECTRICITY_CURRENT_DAY_RATES, {"mpan": "ABC"})
        )
        self.assertEqual(coord._rates, {})


# ---------------------------------------------------------------------------
# Lifecycle (event subscription)
# ---------------------------------------------------------------------------
class LifecycleTests(unittest.TestCase):
    def test_start_subscribes_and_stop_unsubscribes(self) -> None:
        coord = _coordinator("event.x")
        self.assertIsNone(coord._unsub_events)
        coord.async_start()
        self.assertIsNotNone(coord._unsub_events)
        # One listener registered for each of the three day-rate event types.
        registered = set(coord.hass.bus.handlers.keys())
        self.assertEqual(
            registered,
            {
                EVENT_ELECTRICITY_CURRENT_DAY_RATES,
                EVENT_ELECTRICITY_NEXT_DAY_RATES,
                EVENT_ELECTRICITY_PREVIOUS_DAY_RATES,
            },
        )
        coord.async_stop()
        self.assertIsNone(coord._unsub_events)

    def test_start_with_no_entity_is_noop(self) -> None:
        coord = _coordinator("")
        coord.async_start()
        self.assertIsNone(coord._unsub_events)
        self.assertEqual(coord.hass.bus.handlers, {})


# ---------------------------------------------------------------------------
# Coordinator selection in __init__.py
# ---------------------------------------------------------------------------
def _load_init_module():
    """Execute the package ``__init__.py`` (which test_dry_run replaced with a
    bare module) so its ``_create_price_coordinator`` helper is importable."""
    import importlib.util

    path = test_dry_run.PKG_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "octopus_battery",
        path,
        submodule_search_locations=[str(test_dry_run.PKG_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "octopus_battery"
    mod.__path__ = [str(test_dry_run.PKG_DIR)]
    sys.modules["octopus_battery"] = mod
    spec.loader.exec_module(mod)
    return mod


class CoordinatorSelectionTests(unittest.TestCase):
    def test_selects_homeassistant_coordinator(self) -> None:
        init_mod = _load_init_module()
        hass = _FakeHass()
        entry = _FakeEntry(
            {
                CONF_PRICE_SOURCE: PRICE_SOURCE_HOMEASSISTANT,
                CONF_PRICE_ENTITY: "event.octopus_energy_123_ABC_current_day_rates",
                CONF_PRODUCT_CODE: "AGILE-24-10-01",
                CONF_TARIFF_CODE: "E-1R-AGILE-24-10-01-A",
            }
        )
        coord = init_mod._create_price_coordinator(hass, entry)
        self.assertIsInstance(coord, HomeAssistantRatesCoordinator)
        # Started immediately so it subscribes to rate events.
        self.assertIsNotNone(coord._unsub_events)

    def test_selects_api_coordinator_by_default(self) -> None:
        init_mod = _load_init_module()
        hass = _FakeHass()
        entry = _FakeEntry(
            {
                CONF_PRODUCT_CODE: "AGILE-24-10-01",
                CONF_TARIFF_CODE: "E-1R-AGILE-24-10-01-A",
                CONF_PRICE_SOURCE: PRICE_SOURCE_API,
            }
        )
        coord = init_mod._create_price_coordinator(hass, entry)
        self.assertIsInstance(coord, OctopusPriceCoordinator)

    def test_defaults_to_api_when_unconfigured(self) -> None:
        init_mod = _load_init_module()
        hass = _FakeHass()
        entry = _FakeEntry(
            {
                CONF_PRODUCT_CODE: "AGILE-24-10-01",
                CONF_TARIFF_CODE: "E-1R-AGILE-24-10-01-A",
            }
        )
        coord = init_mod._create_price_coordinator(hass, entry)
        self.assertIsInstance(coord, OctopusPriceCoordinator)


# ---------------------------------------------------------------------------
# Config-flow validation
# ---------------------------------------------------------------------------
class ConfigFlowValidationTests(unittest.TestCase):
    def test_price_entity_required_for_homeassistant_source(self) -> None:
        base = {
            "battery_switch": "switch.battery",
            "shelly_switch": "switch.shelly",
            "soc_sensor": "sensor.soc",
        }
        errors = config_flow._validate({**base, "price_source": PRICE_SOURCE_HOMEASSISTANT})
        self.assertEqual(errors.get("price_entity"), "required")

    def test_price_entity_accepted_when_provided(self) -> None:
        base = {
            "battery_switch": "switch.battery",
            "shelly_switch": "switch.shelly",
            "soc_sensor": "sensor.soc",
        }
        errors = config_flow._validate(
            {
                **base,
                "price_source": PRICE_SOURCE_HOMEASSISTANT,
                "price_entity": "event.octopus_energy_123_ABC_current_day_rates",
            }
        )
        self.assertNotIn("price_entity", errors)

    def test_api_source_does_not_require_price_entity(self) -> None:
        base = {
            "battery_switch": "switch.battery",
            "shelly_switch": "switch.shelly",
            "soc_sensor": "sensor.soc",
        }
        errors = config_flow._validate({**base, "price_source": PRICE_SOURCE_API})
        self.assertNotIn("price_entity", errors)

    def test_defaults_to_api_when_absent(self) -> None:
        base = {
            "battery_switch": "switch.battery",
            "shelly_switch": "switch.shelly",
            "soc_sensor": "sensor.soc",
        }
        # No price_source key -> treated as the API source, no entity required.
        errors = config_flow._validate(dict(base))
        self.assertNotIn("price_entity", errors)


if __name__ == "__main__":
    unittest.main()
