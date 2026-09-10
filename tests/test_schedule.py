"""Standalone unit tests for the pure scheduling logic.

Run with:  python3 -m unittest discover -s tests -v
(No Home Assistant installation required - only schedule.py + const.py.)
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import const.py + schedule.py without executing the package __init__.py
# (which requires Home Assistant). Register a stub package with a __path__
# so the normal import machinery resolves the submodules.
ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "octopus_battery"

if "octopus_battery" not in sys.modules:
    _pkg = types.ModuleType("octopus_battery")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["octopus_battery"] = _pkg

from octopus_battery.const import (  # noqa: E402
    MODE_CHARGING,
    MODE_DISCHARGING,
    MODE_IDLE,
    MODE_TOPUP,
)
from octopus_battery.schedule import (  # noqa: E402
    Block,
    PricePoint,
    block_contains,
    decide_mode,
    find_extreme_block,
    select_schedule,
    switches_for_mode,
)

TZ = timezone.utc
BASE = datetime(2025, 6, 1, 0, 0, tzinfo=TZ)


def make_prices(values: list[float], start: datetime = BASE) -> list[PricePoint]:
    """Build contiguous hourly price points from a list of values."""
    points = []
    for i, v in enumerate(values):
        valid_from = start + timedelta(hours=i)
        points.append(
            PricePoint(valid_from, valid_from + timedelta(hours=1), v)
        )
    return points


class FindExtremeBlockTest(unittest.TestCase):
    def test_max_block(self):
        # 24h of 10 p/kWh with a 4h spike of 100 p/kWh at index 16 (16:00-20:00)
        values = [10.0] * 24
        for i in range(16, 20):
            values[i] = 100.0
        prices = make_prices(values)
        block = find_extreme_block(prices, 4, find_max=True)
        self.assertIsNotNone(block)
        self.assertEqual(block.start, BASE + timedelta(hours=16))
        self.assertEqual(block.end, BASE + timedelta(hours=20))
        self.assertEqual(block.total, 400.0)

    def test_min_block(self):
        values = [100.0] * 24
        for i in range(2, 6):
            values[i] = 5.0
        prices = make_prices(values)
        block = find_extreme_block(prices, 4, find_max=False)
        self.assertIsNotNone(block)
        self.assertEqual(block.start, BASE + timedelta(hours=2))
        self.assertEqual(block.end, BASE + timedelta(hours=6))
        self.assertEqual(block.total, 20.0)

    def test_not_enough_points(self):
        prices = make_prices([10.0, 10.0])
        self.assertIsNone(find_extreme_block(prices, 4, find_max=True))

    def test_gap_breaks_contiguity(self):
        # 4 hours, but a 2h gap between point 1 and 2 -> no 4h contiguous block
        p1 = PricePoint(BASE, BASE + timedelta(hours=1), 10.0)
        p2 = PricePoint(BASE + timedelta(hours=1), BASE + timedelta(hours=2), 10.0)
        p3 = PricePoint(BASE + timedelta(hours=4), BASE + timedelta(hours=5), 10.0)
        p4 = PricePoint(BASE + timedelta(hours=5), BASE + timedelta(hours=6), 10.0)
        self.assertIsNone(find_extreme_block([p1, p2, p3, p4], 4, find_max=True))
        # But a 2h block still works on each side of the gap
        block = find_extreme_block([p1, p2, p3, p4], 2, find_max=True)
        self.assertIsNotNone(block)

    def test_window_of_1(self):
        prices = make_prices([1.0, 5.0, 3.0])
        block = find_extreme_block(prices, 1, find_max=True)
        self.assertEqual(block.start, BASE + timedelta(hours=1))
        self.assertEqual(block.total, 5.0)


class SelectScheduleTest(unittest.TestCase):
    def _sel(self, now, use_hours=4, charge_hours=4):
        prices = make_prices([10.0] * 48)
        return select_schedule(
            prices, use_hours=use_hours, charge_hours=charge_hours, now=now
        )

    def test_blocks_anchored_to_current_day(self):
        # 48h of prices. A 4h spike of 50 at BASE (today 00:00) and a much
        # bigger 4h spike of 200 at BASE+40h (tomorrow 16:00). Because blocks
        # are anchored to the *current* day, today's 50-spike is the use block
        # and tomorrow's 200-spike is ignored until tomorrow.
        values = [10.0] * 48
        for i in range(0, 4):
            values[i] = 50.0
        for i in range(40, 44):
            values[i] = 200.0
        prices = make_prices(values)
        now = BASE + timedelta(hours=12)  # today 12:00
        use, charge = select_schedule(
            prices, use_hours=4, charge_hours=4, now=now
        )
        self.assertEqual(use.start, BASE)              # today's 50-spike
        self.assertEqual(use.total, 200.0)
        # Cheapest 4h block starting today: any 4x10 window; first is 04:00.
        self.assertEqual(charge.start, BASE + timedelta(hours=4))
        self.assertEqual(charge.total, 40.0)

    def test_yesterday_blocks_excluded(self):
        # Cheap block (1.0) at day1 20:00-24:00. Now is day2 06:00, so that
        # block is yesterday's and must not be selected; today's cheapest 4h
        # (all 10.0) starts at day2 00:00.
        values = [10.0] * 48
        for i in range(20, 24):
            values[i] = 1.0
        prices = make_prices(values)
        now = BASE + timedelta(hours=30)  # day2 06:00
        use, charge = select_schedule(
            prices, use_hours=4, charge_hours=4, now=now
        )
        self.assertEqual(charge.start, BASE + timedelta(hours=24))  # day2 00:00
        self.assertEqual(charge.total, 40.0)

    def test_block_can_cross_midnight(self):
        # Make the last 2 hours of today and first 2 hours of tomorrow the
        # cheapest -> a 4h charge block 22:00-02:00 crossing midnight.
        values = [10.0] * 48
        values[22] = 1.0  # today 22:00
        values[23] = 1.0  # today 23:00
        values[24] = 1.0  # tomorrow 00:00
        values[25] = 1.0  # tomorrow 01:00
        prices = make_prices(values)
        now = BASE + timedelta(hours=12)
        use, charge = select_schedule(
            prices, use_hours=4, charge_hours=4, now=now
        )
        self.assertEqual(charge.start, BASE + timedelta(hours=22))
        self.assertEqual(charge.end, BASE + timedelta(hours=26))
        self.assertEqual(charge.total, 4.0)


class DecideModeTest(unittest.TestCase):
    NOW = BASE + timedelta(hours=12)
    USE = (BASE + timedelta(hours=11), BASE + timedelta(hours=15))
    CHARGE = (BASE + timedelta(hours=20), BASE + timedelta(hours=24))

    def block(self, start, end):
        return Block(start, end, 0.0, 4)

    def decide(self, now=None, soc=80.0, use=None, charge=None, topup_active=False):
        # Default use block is 16:00-20:00, which does NOT contain NOW (12:00),
        # so top-up / idle logic can be exercised outside of any block.
        if use is None:
            use = self.block(BASE + timedelta(hours=16), BASE + timedelta(hours=20))
        return decide_mode(
            now=now or self.NOW,
            soc=soc,
            use_block=use,
            charge_block=charge,
            discharge_stop_soc=5,
            topup_trigger_soc=3,
            topup_target_soc=5,
            charge_target_soc=100,
            topup_active=topup_active,
        )

    def test_discharging_inside_use_block(self):
        mode, active = self.decide(soc=80.0, use=self.block(*self.USE))
        self.assertEqual(mode, MODE_DISCHARGING)
        self.assertFalse(active)

    def test_use_block_stops_at_threshold(self):
        mode, _ = self.decide(soc=5.0, use=self.block(*self.USE))
        self.assertEqual(mode, MODE_IDLE)
        mode, _ = self.decide(soc=4.0, use=self.block(*self.USE))
        self.assertEqual(mode, MODE_IDLE)
        mode, _ = self.decide(soc=5.1, use=self.block(*self.USE))
        self.assertEqual(mode, MODE_DISCHARGING)

    def test_charging_inside_charge_block(self):
        now = BASE + timedelta(hours=21)
        use = self.block(BASE + timedelta(hours=11), BASE + timedelta(hours=15))
        charge = self.block(*self.CHARGE)
        mode, _ = decide_mode(
            now=now,
            soc=40.0,
            use_block=use,
            charge_block=charge,
            discharge_stop_soc=5,
            topup_trigger_soc=3,
            topup_target_soc=5,
            charge_target_soc=100,
            topup_active=False,
        )
        self.assertEqual(mode, MODE_CHARGING)

    def test_charge_block_done_at_target(self):
        now = BASE + timedelta(hours=21)
        use = self.block(BASE + timedelta(hours=11), BASE + timedelta(hours=15))
        charge = self.block(*self.CHARGE)
        mode, _ = decide_mode(
            now=now,
            soc=100.0,
            use_block=use,
            charge_block=charge,
            discharge_stop_soc=5,
            topup_trigger_soc=3,
            topup_target_soc=5,
            charge_target_soc=100,
            topup_active=False,
        )
        self.assertEqual(mode, MODE_IDLE)

    def test_topup_hysteresis(self):
        # Outside both blocks: below trigger -> top up starts
        mode, active = self.decide(soc=2.0)
        self.assertEqual(mode, MODE_TOPUP)
        self.assertTrue(active)
        # In the band [trigger, target) with no active session -> idle
        mode, active = self.decide(soc=4.0)
        self.assertEqual(mode, MODE_IDLE)
        self.assertFalse(active)
        # Active session continues until target reached
        mode, active = self.decide(soc=4.0, topup_active=True)
        self.assertEqual(mode, MODE_TOPUP)
        self.assertTrue(active)
        # Target reached -> idle and session cleared
        mode, active = self.decide(soc=5.0, topup_active=True)
        self.assertEqual(mode, MODE_IDLE)
        self.assertFalse(active)

    def test_unknown_soc_holds_idle(self):
        mode, active = self.decide(soc=None)
        self.assertEqual(mode, MODE_IDLE)
        self.assertFalse(active)

    def test_no_blocks_only_topup_logic(self):
        mode, active = self.decide(soc=1.0, use=None, charge=None)
        self.assertEqual(mode, MODE_TOPUP)
        mode, active = self.decide(soc=50.0, use=None, charge=None)
        self.assertEqual(mode, MODE_IDLE)

    def test_use_block_priority_over_charge(self):
        # A moment inside BOTH blocks: use block wins.
        use = self.block(BASE + timedelta(hours=10), BASE + timedelta(hours=26))
        charge = self.block(BASE + timedelta(hours=11), BASE + timedelta(hours=27))
        mode, _ = decide_mode(
            now=self.NOW,
            soc=80.0,
            use_block=use,
            charge_block=charge,
            discharge_stop_soc=5,
            topup_trigger_soc=3,
            topup_target_soc=5,
            charge_target_soc=100,
            topup_active=False,
        )
        self.assertEqual(mode, MODE_DISCHARGING)


class SwitchesForModeTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(switches_for_mode(MODE_CHARGING), (True, True))
        self.assertEqual(switches_for_mode(MODE_TOPUP), (True, True))
        self.assertEqual(switches_for_mode(MODE_DISCHARGING), (False, False))
        self.assertEqual(switches_for_mode(MODE_IDLE), (False, True))


class HalfHourlyTest(unittest.TestCase):
    """The current Agile tariff is priced half-hourly; block selection must
    still pick the correct *duration* (e.g. a 4h block = 8 half-hour slots).
    """

    def make_half_hourly(self, values, start=BASE):
        points = []
        for i, v in enumerate(values):
            valid_from = start + timedelta(minutes=30 * i)
            points.append(
                PricePoint(valid_from, valid_from + timedelta(minutes=30), v)
            )
        return points

    def test_4h_block_from_half_hourly(self):
        # 48 half-hour slots (24h). A 4h (8-slot) spike of 100p starting at
        # 16:00 (slot 32).
        values = [10.0] * 48
        for i in range(32, 40):
            values[i] = 100.0
        prices = self.make_half_hourly(values)
        block = find_extreme_block(prices, 4.0, find_max=True)
        self.assertIsNotNone(block)
        self.assertEqual(block.start, BASE + timedelta(hours=16))
        self.assertEqual(block.end, BASE + timedelta(hours=20))
        self.assertEqual(block.total, 800.0)  # 8 slots x 100
        self.assertEqual(block.hours, 4.0)

    def test_cheapest_2h_block_from_half_hourly(self):
        values = [100.0] * 48
        # 2h (4-slot) cheap window at 02:00 (slots 4-7)
        for i in range(4, 8):
            values[i] = 5.0
        prices = self.make_half_hourly(values)
        block = find_extreme_block(prices, 2.0, find_max=False)
        self.assertIsNotNone(block)
        self.assertEqual(block.start, BASE + timedelta(hours=2))
        self.assertEqual(block.end, BASE + timedelta(hours=4))
        self.assertEqual(block.total, 20.0)

    def test_select_schedule_half_hourly_day_anchored(self):
        values = [10.0] * 96  # 48h of half-hourly slots
        # expensive 4h at today 16:00 (slots 32-39)
        for i in range(32, 40):
            values[i] = 50.0
        # cheap 4h at today 00:00 (slots 0-7)
        for i in range(0, 8):
            values[i] = 5.0
        prices = self.make_half_hourly(values)
        now = BASE + timedelta(hours=12)
        use, charge = select_schedule(
            prices, use_hours=4, charge_hours=4, now=now
        )
        self.assertEqual(use.start, BASE + timedelta(hours=16))
        self.assertEqual(use.end, BASE + timedelta(hours=20))
        self.assertEqual(charge.start, BASE)
        self.assertEqual(charge.end, BASE + timedelta(hours=4))


class BlockContainsTest(unittest.TestCase):
    def test_boundaries(self):
        start = BASE
        end = BASE + timedelta(hours=4)
        block = Block(start, end, 0.0, 4)
        self.assertTrue(block_contains(block, start))
        self.assertFalse(block_contains(block, end))
        self.assertTrue(block_contains(block, start + timedelta(hours=3, minutes=59)))
        self.assertFalse(block_contains(None, start))


if __name__ == "__main__":
    unittest.main(verbosity=2)
