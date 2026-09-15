"""Regression test for the midnight day-rollover bug.

Reproduces the incident: at 00:00 the schedule anchor flips to the new day,
but the price data published just before midnight (windowed to only the
first ``max_block + 1`` hours of the new day) made the "cheapest block
starting today" come from a truncated slice - a 00:00-04:00 block that was
not the new day's true cheapest window - so the battery started charging at
midnight.

Fixes under test (pure schedule module, no Home Assistant imports):
  1. ``combine_price_points`` keeps the full next day (window now spans the
     full current day and the full next day).
  2. Just after midnight, the charge block is the new day's true cheapest
     window, and 00:00 itself is not inside it.
  3. A tail-truncated (partial-day) curve yields no blocks at all instead
     of a garbage block (coverage guard in ``select_schedule``), while a
     curve that only starts mid-day still produces blocks (no false
     unknown), a curve missing only the final hour still produces blocks
     (real-world shape: source publishes slots up to ~now), and a
     naive/aware datetime mix does not raise.
  4. Mid-day and past-midnight (23:00-03:00) blocks still work.
"""

import sys
import types
from datetime import datetime, timedelta

# schedule.py only needs .const (pure), but importing through the package
# would execute octopus_battery/__init__.py (voluptuous etc.), so register a
# synthetic package instead.
pkg = types.ModuleType("octopus_battery")
pkg.__path__ = ["./octopus_battery"]
sys.modules["octopus_battery"] = pkg

from octopus_battery.schedule import (  # noqa: E402
    PricePoint,
    block_contains,
    combine_price_points,
    select_schedule,
)

DAY0 = datetime(2026, 9, 14)
DAY1 = DAY0 + timedelta(days=1)
DAYM1 = DAY0 - timedelta(days=1)
failures = []


def slot(day, slot_i, value):
    t0 = day + timedelta(hours=slot_i / 2)
    return PricePoint(t0, t0 + timedelta(minutes=30), value)


def day_curve(day, cheap_from=None, cheap_to=None, cheap_value=10.0, base=20.0):
    """Half-hourly points for one full day; slots [cheap_from, cheap_to)
    (30-min slot indices, may run into the next day) get the cheap value."""
    out = []
    for s in range(48):
        t0 = day + timedelta(hours=s / 2)
        global_idx = s
        if cheap_from is not None and global_idx < 48 and cheap_from <= global_idx < cheap_to:
            value = cheap_value
        else:
            value = base
        out.append(PricePoint(t0, t0 + timedelta(minutes=30), value))
    return out


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}: {detail}")
        print(f"  FAIL: {name}: {detail}")


# -------------------------------------------------------------------------
# 1. Window keeps the full next day
# -------------------------------------------------------------------------
# Pre-midnight poll data: look-behind hour + full DAY0 + full DAY1.
curve = (
    [slot(DAYM1, s, 20.0) for s in (46, 47)]
    + day_curve(DAY0)
    + day_curve(DAY1)
)
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=23, minutes=30), max_block_hours=4
)
check(
    "window includes the full next day",
    any(p.valid_from == DAY1 + timedelta(hours=23, minutes=30) for p in windowed),
    f"last point: {windowed[-1].valid_from if windowed else None}",
)

# -------------------------------------------------------------------------
# 2. Just after midnight: true cheapest window of the new day, not 00:00-04:00
# -------------------------------------------------------------------------
# DAY1's cheap window is 07:00-11:00 (slots 14-22). The data the 23:30
# poll published (with the extended window) is still in coordinator.data at
# 00:05 - it already contains the full DAY1.
curve = (
    [slot(DAYM1, s, 20.0) for s in (46, 47)]
    + day_curve(DAY0)
    + day_curve(DAY1, cheap_from=14, cheap_to=22)
)
windowed = combine_price_points(
    curve, now=DAY1 + timedelta(minutes=5), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY1 + timedelta(minutes=5)
)
check(
    "charge block is the new day's true cheapest window (07:00-11:00)",
    charge_block is not None and charge_block.start == DAY1 + timedelta(hours=7),
    f"got {charge_block.start if charge_block else None}",
)
check(
    "00:00 itself is NOT inside the charge block (no midnight charging)",
    charge_block is not None and not block_contains(charge_block, DAY1),
)

# -------------------------------------------------------------------------
# 3. Tail-truncated curve -> no blocks (coverage guard)
# -------------------------------------------------------------------------
# The old bug shape: only the first 5 h of the new day are visible.
truncated = [p for p in windowed if p.valid_from < DAY1 + timedelta(hours=5)]
use_block, charge_block = select_schedule(
    truncated, use_hours=4, charge_hours=4, now=DAY1 + timedelta(minutes=5)
)
check(
    "partial day -> no blocks (no garbage midnight block)",
    use_block is None and charge_block is None,
    f"got use={use_block}, charge={charge_block}",
)

# -------------------------------------------------------------------------
# 3b. Curve starting mid-day (entity truncated to "now" onwards) -> blocks
#     are STILL computed from the visible part (no false unknown).
# -------------------------------------------------------------------------
# DAY0 from 09:00 (slots 18-47) plus the full DAY1. All points are 20.0,
# so the cheapest 4 h window is the first fully-visible one (09:00-13:00).
curve = [slot(DAY0, s, 20.0) for s in range(18, 48)] + day_curve(DAY1)
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=12), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY0 + timedelta(hours=12)
)
check(
    "mid-day start curve -> blocks still computed from visible part",
    charge_block is not None and charge_block.start >= DAY0 + timedelta(hours=9),
    f"got {charge_block.start if charge_block else None}",
)

# -------------------------------------------------------------------------
# 3c. Naive vs aware datetime mix -> no exception, no blocks.
# -------------------------------------------------------------------------
from datetime import timezone as _tz  # noqa: E402

naive_curve = [slot(DAY0, s, 20.0) for s in range(48)]  # naive datetimes
aware_now = DAY0.replace(tzinfo=_tz.utc) + timedelta(hours=12)
use_block, charge_block = select_schedule(
    naive_curve, use_hours=4, charge_hours=4, now=aware_now
)
check(
    "naive/aware mix -> (None, None) without raising",
    use_block is None and charge_block is None,
)

# -------------------------------------------------------------------------
# 3d. Real-world shape: day minus the final hour (source publishes slots
#     up to ~now and never publishes the tail) -> blocks ARE computed from
#     the fully-covered windows (missing tail < max_block is tolerated).
# -------------------------------------------------------------------------
# 2 look-behind slots + DAY0 slots 0-45 (00:00-22:30, last valid_to 23:00).
curve = [
    slot(DAYM1, s, 20.0) for s in (46, 47)
] + [slot(DAY0, s, 20.0) for s in range(46)]
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=12), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY0 + timedelta(hours=12)
)
check(
    "day minus final hour -> block computed from fully-covered windows",
    charge_block is not None
    and charge_block.start == DAY0
    and charge_block.end == DAY0 + timedelta(hours=4),
    f"got {charge_block.start if charge_block else None}-{charge_block.end if charge_block else None}",
)

# -------------------------------------------------------------------------
# 3e. Boundary: curve ends exactly at day_end - max_block -> still OK.
# -------------------------------------------------------------------------
# DAY0 slots 0-39 (00:00-19:30, last valid_to 20:00 = day_end - 4 h).
curve = [
    slot(DAYM1, s, 20.0) for s in (46, 47)
] + [slot(DAY0, s, 20.0) for s in range(40)]
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=12), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY0 + timedelta(hours=12)
)
check(
    "curve ending exactly at day_end - max_block -> block computed",
    charge_block is not None and charge_block.start == DAY0,
    f"got {charge_block.start if charge_block else None}",
)

# -------------------------------------------------------------------------
# 4a. Mid-day: cheapest 4h window of the current day
# -------------------------------------------------------------------------
curve = [slot(DAYM1, s, 20.0) for s in (46, 47)] + day_curve(DAY0, cheap_from=14, cheap_to=22) + day_curve(DAY1)
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=12), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY0 + timedelta(hours=12)
)
check(
    "mid-day: charge block is the day's cheapest window (07:00-11:00)",
    charge_block is not None and charge_block.start == DAY0 + timedelta(hours=7),
    f"got {charge_block.start if charge_block else None}",
)

# -------------------------------------------------------------------------
# 4b. Past-midnight block (23:00-03:00) still selected
# -------------------------------------------------------------------------
# Cheap 4 h: DAY0 23:00-24:00 + DAY1 00:00-03:00.
curve = (
    [slot(DAYM1, s, 20.0) for s in (46, 47)]
    + [slot(DAY0, s, 5.0 if s >= 46 else 20.0) for s in range(48)]
    + [slot(DAY1, s, 5.0 if s < 6 else 20.0) for s in range(48)]
)
windowed = combine_price_points(
    curve, now=DAY0 + timedelta(hours=23, minutes=30), max_block_hours=4
)
use_block, charge_block = select_schedule(
    windowed, use_hours=4, charge_hours=4, now=DAY0 + timedelta(hours=23, minutes=30)
)
check(
    "past-midnight block 23:00-03:00 still selected",
    charge_block is not None
    and charge_block.start == DAY0 + timedelta(hours=23)
    and charge_block.end == DAY1 + timedelta(hours=3),
    f"got {charge_block.start if charge_block else None}-{charge_block.end if charge_block else None}",
)

# -------------------------------------------------------------------------
if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK: midnight rollover produces the correct new-day schedule and no")
print("OK: garbage block from a partial day; mid-day / past-midnight blocks unchanged")
