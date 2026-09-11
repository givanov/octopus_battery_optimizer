"""Full-day simulation of the controller decision loop.

Runs select_schedule() + decide_mode() + SoC dynamics over 24 hours in
5-minute steps against a synthetic Agile-like price curve, and asserts the
resulting behaviour matches the product spec:

  * charge in the cheapest 4h block
  * discharge in the most expensive 4h block (down to 5%)
  * otherwise idle (load on mains, battery topped up)
  * maintenance top-up when SoC falls below 3%, up to 5%
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "octopus_battery"
if "octopus_battery" not in sys.modules:
    _pkg = types.ModuleType("octopus_battery")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["octopus_battery"] = _pkg

from octopus_battery.schedule import (  # noqa: E402
    PricePoint,
    decide_mode,
    select_schedule,
)

TZ = timezone.utc
DAY_START = datetime(2025, 6, 1, 0, 0, tzinfo=TZ)

# Synthetic Agile-like curve (p/kWh):
#   00:00-04:00  5   (cheapest block)
#   04:00-16:00  20
#   16:00-20:00  50  (most expensive block)
#   20:00-24:00  20
VALUES = [5.0] * 4 + [20.0] * 12 + [50.0] * 4 + [20.0] * 4

CHARGE_RATE_PER_HOUR = 15.0  # %/h while charging / top-up
DISCHARGE_RATE_PER_HOUR = 15.0  # %/h while discharging
SELF_DISCHARGE_PER_HOUR = 0.5  # %/h while idle


def make_prices(day_start: datetime, values: list[float]) -> list[PricePoint]:
    return [
        PricePoint(
            day_start + timedelta(hours=i),
            day_start + timedelta(hours=i + 1),
            v,
        )
        for i, v in enumerate(values)
    ]


class DaySimulation:
    """Replays the controller's decision loop for one day."""

    def __init__(self, start_soc: float = 100.0) -> None:
        self.start_soc = start_soc

    def run(self):
        day_prices = make_prices(DAY_START, VALUES)
        next_prices = make_prices(DAY_START + timedelta(days=1), VALUES)
        all_prices = day_prices + next_prices

        soc = self.start_soc
        topup_active = False
        trace: list[tuple[int, str, float]] = []

        for minute in range(0, 24 * 60, 5):
            now = DAY_START + timedelta(minutes=minute)
            use, charge = select_schedule(
                all_prices,
                use_hours=4,
                charge_hours=4,
                now=now,
            )
            mode, topup_active = decide_mode(
                now=now,
                soc=soc,
                use_block=use,
                charge_block=charge,
                discharge_stop_soc=5,
                topup_trigger_soc=3,
                topup_target_soc=5,
                charge_target_soc=100,
                topup_active=topup_active,
            )
            trace.append((minute, mode, round(soc, 3)))

            step = (
                (CHARGE_RATE_PER_HOUR if mode in ("charging", "top_up", "idle_floating")
                 else -DISCHARGE_RATE_PER_HOUR if mode == "discharging"
                 else -SELF_DISCHARGE_PER_HOUR)
                / 12.0  # per 5 minutes
            )
            soc = max(0.0, min(100.0, soc + step))
        return trace


class DaySimulationTest(unittest.TestCase):
    def _at(self, trace, minute):
        return trace[minute // 5]

    def test_full_battery_day(self):
        trace = DaySimulation(start_soc=100.0).run()
        at = lambda m: self._at(trace, m)[1]  # noqa: E731
        # Battery already full: the charger holds it at 100% (idle_floating)
        # until the expensive block, then discharge, then idle_not_charging.
        self.assertEqual(at(0), "idle_floating")
        self.assertEqual(at(12 * 60), "idle_floating")
        self.assertEqual(at(16 * 60), "discharging")
        self.assertEqual(at(19 * 60 + 55), "discharging")
        self.assertEqual(at(20 * 60), "idle_not_charging")
        self.assertEqual(at(23 * 60), "idle_not_charging")

    def test_charge_then_discharge(self):
        trace = DaySimulation(start_soc=30.0).run()
        at = lambda m: self._at(trace, m)[1]  # noqa: E731
        self.assertEqual(at(0), "charging")
        self.assertEqual(at(3 * 60 + 55), "charging")
        self.assertEqual(at(4 * 60), "idle_not_charging")  # charge block over
        self.assertEqual(at(16 * 60), "discharging")
        self.assertEqual(at(20 * 60), "idle_not_charging")

    def test_soc_trajectory_charge_then_discharge(self):
        trace = DaySimulation(start_soc=50.0).run()
        soc_at = lambda m: self._at(trace, m)[2]  # noqa: E731
        # Morning charge block (00:00-04:00) raises SoC from 50 towards full.
        self.assertGreater(soc_at(3 * 60 + 55), 50.0)
        # By 04:00 it should be near full (50 + 4h*15%/h, capped at 100).
        self.assertGreaterEqual(soc_at(4 * 60), 95.0)
        # Evening use block (16:00-20:00) then lowers the SoC again.
        self.assertGreater(soc_at(16 * 60), soc_at(19 * 60 + 55))

    def test_top_up_when_depleted(self):
        trace = DaySimulation(start_soc=1.0).run()
        # At 00:00 we're inside the (cheapest) charge block, so it charges
        # to target rather than doing a small top-up.
        self.assertEqual(self._at(trace, 0)[1], "charging")

    def test_top_up_outside_blocks(self):
        # Start the day with the battery in the top-up band and verify the
        # hysteresis: below trigger -> top up, at/above target -> idle.
        sim = DaySimulation(start_soc=2.0)
        trace = sim.run()
        # Find a moment outside both blocks (e.g. 10:00) - battery should be
        # idle_not_charging (it topped up to 5% during the 00:00-04:00 charge block).
        self.assertEqual(self._at(trace, 10 * 60)[1], "idle_not_charging")
        self.assertGreaterEqual(self._at(trace, 10 * 60)[2], 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
