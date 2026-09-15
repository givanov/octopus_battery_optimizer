"""Pure scheduling and mode-decision logic for the battery optimizer.

This module intentionally has **no Home Assistant imports** so it can be
unit-tested standalone. All datetimes are timezone-aware *local* times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from .const import (
    MODE_CHARGING,
    MODE_DISCHARGING,
    MODE_IDLE_FLOATING,
    MODE_IDLE_NOT_CHARGING,
    MODE_TOPUP,
)


@dataclass(frozen=True)
class PricePoint:
    """One hourly price point from the Octopus API (local time)."""

    valid_from: datetime
    valid_to: datetime
    value: float  # pence per kWh (incl. VAT)


@dataclass(frozen=True)
class Block:
    """A contiguous block of hours selected from the price curve."""

    start: datetime
    end: datetime
    total: float
    hours: int


def is_contiguous(points: Sequence[PricePoint]) -> bool:
    """Return True if *points* form an unbroken chain of hours."""
    for i in range(1, len(points)):
        if points[i].valid_from != points[i - 1].valid_to:
            return False
    return True


def find_extreme_block(
    prices: Sequence[PricePoint],
    duration_hours: float,
    *,
    find_max: bool,
    start_lo: Optional[datetime] = None,
    start_hi: Optional[datetime] = None,
) -> Optional[Block]:
    """Find the contiguous window of price points spanning ``duration_hours``
    with the highest (find_max=True) or lowest (find_max=False) total value.

    The window is matched by *duration*, not point count, so it works
    correctly whether the tariff is priced hourly or half-hourly (the
    current Agile granularity).

    ``start_lo`` / ``start_hi`` (if given) constrain where the block may
    *start*: only windows whose first point has ``start_lo <= valid_from <
    start_hi`` are considered. This lets callers anchor blocks to a
    calendar day while still allowing the block to extend past the day's
    end (e.g. a 23:00-03:00 block).

    Returns None if no suitable contiguous window exists.
    """
    if duration_hours <= 0 or not prices:
        return None

    target = timedelta(hours=duration_hours)
    n = len(prices)
    first_dur = prices[0].valid_to - prices[0].valid_from
    if first_dur <= timedelta(0):
        return None

    # Uniform granularity is the real case (hourly or half-hourly); derive
    # how many consecutive points span the target duration.
    n_points = int(round(target.total_seconds() / first_dur.total_seconds()))
    if n_points < 1 or n < n_points:
        return None

    best: Optional[Block] = None
    for i in range(n - n_points + 1):
        window = prices[i : i + n_points]
        if not is_contiguous(window):
            continue
        span = window[-1].valid_to - window[0].valid_from
        if abs(span - target) > timedelta(minutes=1):
            continue  # mixed granularity that doesn't line up
        start = window[0].valid_from
        if start_lo is not None and start < start_lo:
            continue
        if start_hi is not None and start >= start_hi:
            continue
        total = sum(p.value for p in window)
        candidate = Block(
            start=start,
            end=window[-1].valid_to,
            total=total,
            hours=duration_hours,
        )
        if best is None or (total > best.total if find_max else total < best.total):
            best = candidate
    return best


def select_schedule(
    prices: Sequence[PricePoint],
    *,
    use_hours: int,
    charge_hours: int,
    now: datetime,
) -> tuple[Optional[Block], Optional[Block]]:
    """Select today's use (most expensive) and charge (cheapest) blocks.

    The schedule is anchored to the current calendar day (local time):
    both blocks must *start* within ``[today 00:00, tomorrow 00:00)``. This
    keeps the schedule stable for the whole day (so we never abandon a
    block we are already in) while still allowing a block to run past
    midnight. The next day's blocks only become active once that day starts.

    *prices* must be sorted by start time (see ``combine_price_points``).
    If the points do not reach at least ``max_block`` hours before the end
    of the anchored day (a stale or heavily truncated price curve - e.g. a
    pre-midnight window evaluated just after the day rolled over, when
    only the first hours of the new day are visible), both blocks are
    None: a block computed from such a truncated slice is not actually the
    day's most/least expensive window, and the controller then falls back
    to its SoC-based modes instead of acting on a wrong block. A short
    missing tail (less than a full block - e.g. the last hour of the day
    not yet published by the price source) is tolerated: candidate windows
    that cannot be fully covered are simply not considered.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(hours=24)

    max_block_hours = max(float(use_hours), float(charge_hours))
    min_valid_to = day_end - timedelta(hours=max_block_hours)

    if not prices:
        return None, None
    try:
        covered_enough = prices[-1].valid_to >= min_valid_to
    except TypeError:  # naive vs aware datetime - not comparable
        covered_enough = False
    if not covered_enough:
        return None, None

    use_block = find_extreme_block(
        prices,
        float(use_hours),
        find_max=True,
        start_lo=day_start,
        start_hi=day_end,
    )
    charge_block = find_extreme_block(
        prices,
        float(charge_hours),
        find_max=False,
        start_lo=day_start,
        start_hi=day_end,
    )
    return use_block, charge_block


def block_contains(block: Optional[Block], moment: datetime) -> bool:
    """True if *moment* falls inside the block (start inclusive, end exclusive)."""
    return block is not None and block.start <= moment < block.end


def combine_price_points(
    points: Iterable[PricePoint],
    *,
    now: datetime,
    max_block_hours: int,
) -> list[PricePoint]:
    """Merge, window and sort price points for block selection.

    Points may come from several sources (e.g. the previous/current/next-day
    rates published by the Octopus Energy integration). Points are
    deduplicated by ``valid_from`` (later entries win, so fresher data
    overrides stale data for the same slot), then restricted to the same
    window the API-based coordinator uses, and returned sorted by start time.

    The window spans from one hour before the start of the current calendar
    day to ``max_block_hours + 1`` hours after the *day after next*'s start
    - i.e. it includes the full current day and the full next day. Including
    the whole next day (not just ``max_block_hours`` past midnight) matters
    at the day boundary: once the schedule anchor rolls over to the new
    day, its blocks need the entire new day, and the data published just
    before midnight (which already contains the next day) must be usable
    for the first ticks of the new day. Pure - no Home Assistant imports.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    keep_from = day_start - timedelta(hours=1)
    keep_to = day_start + timedelta(hours=48 + max_block_hours + 1)

    by_start: dict[datetime, PricePoint] = {}
    for point in points:
        if keep_from <= point.valid_from <= keep_to:
            by_start[point.valid_from] = point

    return [by_start[key] for key in sorted(by_start)]


def decide_mode(
    *,
    now: datetime,
    soc: Optional[float],
    use_block: Optional[Block],
    charge_block: Optional[Block],
    discharge_stop_soc: int,
    topup_trigger_soc: int,
    topup_target_soc: int,
    charge_target_soc: int,
    topup_active: bool,
) -> tuple[str, bool]:
    """Decide the controller mode for the current moment.

    Returns a ``(mode, topup_active)`` tuple. ``topup_active`` is the new
    hysteresis state that must be persisted between calls.

    Priority order:
      1. Inside the use block    -> discharge while SoC > stop threshold,
                                    else idle_not_charging (drained, awaiting charge)
      2. Inside the charge block -> charge while SoC < charge target,
                                    else idle_floating (at target, holding)
      3. Otherwise:
         - SoC >= charge target  -> idle_floating (hold at 100% until the next
                                    discharge cycle)
         - SoC >= top-up target  -> idle_not_charging (resting on the mains)
         - active top-up session or SoC < top-up trigger -> top_up

    If ``soc`` is unknown the controller holds idle_not_charging (load stays on
    mains, battery left uncharged).
    """
    if soc is None:
        return MODE_IDLE_NOT_CHARGING, topup_active

    if block_contains(use_block, now):
        if soc > discharge_stop_soc:
            return MODE_DISCHARGING, topup_active
        return MODE_IDLE_NOT_CHARGING, topup_active

    if block_contains(charge_block, now):
        if soc < charge_target_soc:
            return MODE_CHARGING, topup_active
        return MODE_IDLE_FLOATING, topup_active

    # Outside both blocks.
    if soc >= charge_target_soc:
        # Floating: charger holds the battery at 100% until the next discharge cycle.
        return MODE_IDLE_FLOATING, False
    if soc >= topup_target_soc:
        return MODE_IDLE_NOT_CHARGING, False
    if topup_active:
        return MODE_TOPUP, True
    if soc < topup_trigger_soc:
        return MODE_TOPUP, True
    return MODE_IDLE_NOT_CHARGING, False


def switches_for_mode(mode: str) -> tuple[bool, bool]:
    """Map a mode to (battery_plug_on, shelly_on).

    * battery plug ON  -> battery is connected to mains and charging
    * shelly ON        -> load is fed from mains
    * shelly OFF       -> load is fed from the battery
    """
    if mode == MODE_CHARGING:
        return True, True
    if mode == MODE_TOPUP:
        return True, True
    if mode == MODE_IDLE_FLOATING:
        # Charger stays connected (float/maintenance) so the battery holds
        # 100% until the next discharge cycle.
        return True, True
    if mode == MODE_DISCHARGING:
        return False, False
    return False, True  # MODE_IDLE_NOT_CHARGING: battery rests, load on the mains
