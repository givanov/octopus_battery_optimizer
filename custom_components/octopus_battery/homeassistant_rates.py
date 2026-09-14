"""Data coordinator that reads electricity prices from the BottlecapDave
``octopus_energy`` Home Assistant integration instead of polling the Octopus
API.

The ``octopus_energy`` integration (https://github.com/BottlecapDave/
HomeAssistant-OctopusEnergy) already polls the Octopus API and publishes each
day's rates as *event entities* (e.g. ``event.octopus_energy_<serial>_<mpan>
_current_day_rates``). Those event entities carry, in their state attributes,
the full day's rates as a ``rates`` list of ``{"start", "end",
"value_inc_vat"}`` objects (plus ``mpan``/``serial_number``/``tariff_code``).
``value_inc_vat`` is expressed in **GBP per kWh** (the integration divides the
API's pence value by 100).

This coordinator:

* reads the configured *current day* event entity plus, best-effort, the
  related *next/previous day* entities (derived from the entity id), so it has
  a continuous curve spanning today into early tomorrow (needed for day-
  anchored blocks that run past midnight);
* subscribes to the integration's rate events to update immediately when new
  rates are published;
* converts every rate to the same ``PricePoint`` format used by the
  API-based coordinator - with the value scaled back to **pence per kWh**
  (``value_inc_vat * 100``) so the ``p/kWh`` sensors stay consistent
  regardless of the price source.

The result is exposed through the standard ``DataUpdateCoordinator``
interface (``.data`` -> ``list[PricePoint]`` and ``.last_update_success``),
so the controller is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHARGE_HOURS,
    CONF_PRICE_ENTITY,
    CONF_USE_HOURS,
    DOMAIN,
    EVENT_ELECTRICITY_DAY_RATES,
    RATE_POLL_MINUTES,
)
from .helpers import effective_data
from .schedule import PricePoint, combine_price_points

_LOGGER = logging.getLogger(__name__)

# Suffix of the "current day rates" event entity; used to derive the related
# next/previous day entities (the source integration names them consistently).
_CURRENT_DAY_SUFFIX = "_current_day_rates"


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a rate boundary that may be a ``datetime`` or an ISO string.

    Event data is ``datetime`` objects when read live, but ISO strings after a
    Home Assistant restart (restored from persistence). Handle both.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return dt_util.parse_datetime(value)
        except (ValueError, TypeError):
            return None
    return None


class HomeAssistantRatesCoordinator(DataUpdateCoordinator[list[PricePoint]]):
    """Read electricity prices from the ``octopus_energy`` integration.

    See the module docstring for the data model. The coordinator exposes the
    same surface the controller relies on (``.data``, ``.last_update_success``
    and ``async_add_listener``), so the rest of the integration is unaware of
    which price source is in use.
    """

    config_entry: Any

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-homeassistant-rates",
            update_interval=timedelta(minutes=RATE_POLL_MINUTES),
            config_entry=entry,
        )
        data = effective_data(entry)
        self._price_entity: str = str(data.get(CONF_PRICE_ENTITY, "") or "")
        # The target meter, learned from the configured entity's attributes.
        # Used to ignore rate events fired for other meters (if any).
        self._mpan: Optional[str] = None
        self._serial: Optional[str] = None
        self._unsub_events: Optional[Callable[[], None]] = None
        # Rate cache keyed by slot start time. Merged from the event entities
        # and from live events; pruned to the relevant window after each
        # update so it stays bounded.
        self._rates: dict[datetime, PricePoint] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def async_start(self) -> None:
        """Subscribe to the integration's rate events.

        One listener is registered per event type (``EventBus.async_listen``
        accepts a single type). Events are the primary (immediate) update
        mechanism; the ``update_interval`` poll is a fallback. Must be called
        before the first refresh so early events are not missed.
        """
        if self._unsub_events is not None or not self._price_entity:
            return
        unsubs = [
            self.hass.bus.async_listen(event_type, self._handle_rate_event)
            for event_type in EVENT_ELECTRICITY_DAY_RATES
        ]

        def _unsub() -> None:
            for unsub in unsubs:
                unsub()

        self._unsub_events = _unsub
        _LOGGER.debug(
            "Subscribed to octopus_energy rate events for %s",
            self._price_entity,
        )

    def async_stop(self) -> None:
        """Unsubscribe from rate events (called on entry unload)."""
        if self._unsub_events is not None:
            self._unsub_events()
            self._unsub_events = None

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------
    def _handle_rate_event(self, event: Event) -> None:
        """Entry point for rate events (may be invoked from any thread).

        The ``octopus_energy`` integration can fire its rate events from a
        worker thread (its rate refresh runs off the event loop), so this
        listener may be called from a thread other than the event loop. The
        cache merge and ``hass.async_create_task`` must happen on the loop,
        so hop to it first (a direct call when the event was fired on the
        loop).
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Invoked from a worker thread: hop to the event loop.
            loop = self.hass.loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(self._process_rate_event, event)
        else:
            self._process_rate_event(event)

    def _process_rate_event(self, event: Event) -> None:
        """Merge a rate event into the cache and re-publish (event loop only).

        The async re-publish is scheduled as a task (mirroring how the
        controller re-evaluates on price updates).
        """
        data = event.data
        if not data or not isinstance(data.get("rates"), list):
            return
        # Once the target meter is known (learned from the configured entity),
        # ignore rates fired for other meters. Before it's known, events are
        # accepted best-effort; the next entity read overwrites with the
        # authoritative meter's rates.
        if self._mpan is not None and data.get("mpan") and data["mpan"] != self._mpan:
            return
        self._merge_rates(data["rates"])
        self.hass.async_create_task(
            self._async_publish(), name=f"{DOMAIN}-ha-rates-event"
        )

    # ------------------------------------------------------------------
    # Rate parsing / merging
    # ------------------------------------------------------------------
    def _to_price_point(self, rate: dict[str, Any]) -> Optional[PricePoint]:
        """Convert one ``octopus_energy`` rate dict to a ``PricePoint``.

        The integration reports ``value_inc_vat`` in GBP/kWh; scale by 100 to
        pence/kWh so downstream ``p/kWh`` sensors match the API-based source.
        """
        start = _parse_datetime(rate.get("start"))
        end = _parse_datetime(rate.get("end"))
        raw_value = rate.get("value_inc_vat")
        if start is None or end is None or raw_value is None:
            return None
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None
        return PricePoint(
            valid_from=dt_util.as_local(start),
            valid_to=dt_util.as_local(end),
            value=value * 100.0,
        )

    def _merge_rates(self, rates: list[dict[str, Any]]) -> int:
        """Parse and merge a list of rate dicts into the cache.

        Returns the number of usable rate points merged.
        """
        merged = 0
        for rate in rates:
            if not isinstance(rate, dict):
                continue
            point = self._to_price_point(rate)
            if point is not None:
                self._rates[point.valid_from] = point
                merged += 1
        return merged

    def _read_entity(self, entity_id: str) -> list[dict[str, Any]]:
        """Read the ``rates`` attribute from a rate event entity.

        Also learns the target meter (``mpan``/``serial_number``) from the
        configured current-day entity's attributes. Returns an empty list if
        the entity does not exist or has no rates yet.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            return []
        rates = state.attributes.get("rates")
        if not isinstance(rates, list):
            return []
        if entity_id == self._price_entity:
            mpan = state.attributes.get("mpan")
            serial = state.attributes.get("serial_number")
            if mpan:
                self._mpan = str(mpan)
            if serial:
                self._serial = str(serial)
        return rates

    def _related_entity_ids(self) -> list[str]:
        """The configured current-day entity plus derived next/previous ones.

        The next/previous ids are derived by swapping the ``_current_day_rates``
        suffix; if the user renamed the entity (so it no longer ends with that
        suffix) only the configured entity is used.
        """
        ids = [self._price_entity]
        if self._price_entity.endswith(_CURRENT_DAY_SUFFIX):
            base = self._price_entity[: -len(_CURRENT_DAY_SUFFIX)]
            ids.append(base + "_next_day_rates")
            ids.append(base + "_previous_day_rates")
        return ids

    # ------------------------------------------------------------------
    # Windowing / publishing
    # ------------------------------------------------------------------
    def _window_bounds(self) -> tuple[datetime, datetime]:
        """Return the (keep_from, keep_to) window for the current day.

        Matches the API-based coordinator: from one hour before the start of
        the current calendar day to ``max_block + 1`` hours after the next
        day's start.
        """
        data = effective_data(self.config_entry)
        max_block_hours = max(
            int(data.get(CONF_USE_HOURS, 4)),
            int(data.get(CONF_CHARGE_HOURS, 4)),
        )
        day_start = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            day_start - timedelta(hours=1),
            day_start + timedelta(hours=24 + max_block_hours + 1),
        )

    def _windowed(self) -> list[PricePoint]:
        data = effective_data(self.config_entry)
        max_block_hours = max(
            int(data.get(CONF_USE_HOURS, 4)),
            int(data.get(CONF_CHARGE_HOURS, 4)),
        )
        return combine_price_points(
            self._rates.values(),
            now=dt_util.now(),
            max_block_hours=max_block_hours,
        )

    async def _async_publish(self) -> None:
        """Publish the current (windowed) cache to listeners."""
        if not self._rates:
            return
        await self.async_set_updated_data(self._windowed())

    # ------------------------------------------------------------------
    # Coordinator update
    # ------------------------------------------------------------------
    async def _async_update_data(self) -> list[PricePoint]:
        """Read the rate event entities and return the combined price curve."""
        data = effective_data(self.config_entry)
        entity_id = str(data.get(CONF_PRICE_ENTITY, "") or "")
        self._price_entity = entity_id
        if not entity_id:
            raise UpdateFailed(
                "No price entity configured - select the 'current day rates' "
                "event entity from the Octopus Energy (Home Assistant) "
                "integration"
            )

        # Refresh the cache from the event entities (authoritative, and what
        # the integration persists). Live events keep it up to date in between.
        for related_id in self._related_entity_ids():
            self._merge_rates(self._read_entity(related_id))

        if not self._rates:
            raise UpdateFailed(
                f"No rate data found from {entity_id} (or its related "
                f"next/previous day entities). Make sure the Octopus Energy "
                f"(Home Assistant) integration is set up and its day-rates "
                f"event entities exist"
            )

        # Prune to the relevant window so the cache stays bounded.
        keep_from, keep_to = self._window_bounds()
        self._rates = {
            start: point
            for start, point in self._rates.items()
            if keep_from <= start <= keep_to
        }

        _LOGGER.debug(
            "Read %d price points from %s (windowed to %d)",
            len(self._rates),
            entity_id,
            len(self._windowed()),
        )
        return self._windowed()
