"""Data coordinator that fetches hourly prices from the Octopus API.

The standard-unit-rates endpoint is public - no API key is required.

Notes on the current API (verified against api.octopus.energy):
  * Endpoint:  GET /v1/products/{product}/electricity-tariffs/{tariff}/standard-unit-rates
  * Response:  paginated dict  {"count", "next", "previous", "results": [...]}
  * Ordering:  newest first, so the furthest-future prices are on page 1.
  * Granularity: half-hourly for the current Agile tariff (1800 s slots).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHARGE_HOURS,
    CONF_USE_HOURS,
    DOMAIN,
    OCTOPUS_API_BASE,
    OCTOPUS_API_TIMEOUT,
)
from .helpers import effective_data
from .schedule import PricePoint

_LOGGER = logging.getLogger(__name__)

# Safety cap on how many pages we will walk (newest-first) to cover the
# look-behind window. A page holds 100 half-hourly slots = 50 h, so 3 pages
# already cover 150 h of history - far more than the ~24 h we need.
MAX_PAGES = 3


class OctopusPriceCoordinator(DataUpdateCoordinator[list[PricePoint]]):
    """Fetch electricity prices from the Octopus API."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
            config_entry=entry,
        )
        data = effective_data(entry)
        self._product_code: str = data["product_code"]
        self._tariff_code: str = data["tariff_code"]

    def _base_url(self) -> str:
        return (
            f"{OCTOPUS_API_BASE}/products/{self._product_code}"
            f"/electricity-tariffs/{self._tariff_code}/standard-unit-rates"
        )

    async def _fetch_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> dict[str, Any]:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=OCTOPUS_API_TIMEOUT)
        ) as resp:
            if resp.status == 404:
                raise UpdateFailed(
                    "Octopus API returned 404 - check the product code and "
                    "tariff code"
                )
            if resp.status != 200:
                raise UpdateFailed(f"Octopus API returned HTTP {resp.status}")
            return await resp.json()

    async def _async_update_data(self) -> list[PricePoint]:
        data = effective_data(self.config_entry)
        use_hours = int(data.get(CONF_USE_HOURS, 4))
        charge_hours = int(data.get(CONF_CHARGE_HOURS, 4))
        max_block_hours = max(use_hours, charge_hours)

        now = dt_util.now()
        # Day-anchored blocks start within [today 00:00, tomorrow 00:00) and
        # may run up to max_block_hours past midnight, so keep:
        keep_from = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)
        keep_to = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(hours=24 + max_block_hours + 1)
        )

        raw_entries: list[dict[str, Any]] = []
        oldest: datetime | None = None

        async with aiohttp.ClientSession() as session:
            for page in range(1, MAX_PAGES + 1):
                url = self._base_url()
                if page > 1:
                    url = f"{url}?page={page}"
                body = await self._fetch_page(session, url)
                results = body.get("results", [])
                if not results:
                    break
                raw_entries.extend(results)

                page_oldest = min(
                    dt_util.parse_datetime(r["valid_from"]) for r in results
                )
                if oldest is None or page_oldest < oldest:
                    oldest = page_oldest
                # We walk newest-first; stop once we have covered the
                # look-behind window.
                if oldest is not None and oldest <= keep_from:
                    break
                if not body.get("next"):
                    break

        prices: list[PricePoint] = []
        for item in raw_entries:
            try:
                valid_from = dt_util.parse_datetime(item["valid_from"])
                valid_to = dt_util.parse_datetime(item["valid_to"])
                value = float(
                    item.get("value_inc_vat", item.get("value_exc_vat", 0.0))
                )
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.debug("Skipping malformed price entry: %r", item)
                continue
            if valid_from < keep_from or valid_from > keep_to:
                continue
            prices.append(
                PricePoint(
                    valid_from=dt_util.as_local(valid_from),
                    valid_to=dt_util.as_local(valid_to),
                    value=value,
                )
            )

        prices.sort(key=lambda p: p.valid_from)
        if not prices:
            raise UpdateFailed("Octopus API returned no usable price data")

        _LOGGER.debug(
            "Fetched %d price points (%d raw) from %s",
            len(prices),
            len(raw_entries),
            self._product_code,
        )
        return prices
