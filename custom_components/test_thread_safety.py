"""Smoke test: _handle_rate_event must be safe when invoked from a worker thread.

Stubs the homeassistant package (not installed in this dev environment),
then simulates the reported failure mode: the octopus_energy integration
fires a rate event from a ThreadPoolExecutor worker thread.
"""

import asyncio
import importlib.util
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Stub the homeassistant package
# ---------------------------------------------------------------------------
ha = types.ModuleType("homeassistant")
ha_core = types.ModuleType("homeassistant.core")
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_uc = types.ModuleType("homeassistant.helpers.update_coordinator")
ha_util = types.ModuleType("homeassistant.util")
ha_dt = types.ModuleType("homeassistant.util.dt")
ha_config_entries = types.ModuleType("homeassistant.config_entries")


class Event:
    def __init__(self, event_type, data=None):
        self.event_type = event_type
        self.data = data or {}


class HomeAssistant:
    pass


class UpdateFailed(Exception):
    pass


class DataUpdateCoordinator(Generic[T]):
    def __init__(self, hass, logger, name=None, update_interval=None, config_entry=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = False

    async def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        return data


def parse_datetime(value):
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


ha_core.Event = Event
ha_core.HomeAssistant = HomeAssistant
ha_uc.DataUpdateCoordinator = DataUpdateCoordinator
ha_uc.UpdateFailed = UpdateFailed
ha_dt.parse_datetime = parse_datetime
ha_dt.now = lambda: datetime.now(timezone.utc)
ha_dt.as_local = lambda dt: dt
ha_config_entries.ConfigEntry = object

ha.core = ha_core
ha.helpers = ha_helpers
ha.util = ha_util
ha.config_entries = ha_config_entries
ha_helpers.update_coordinator = ha_uc
ha_util.dt = ha_dt

for name, mod in {
    "homeassistant": ha,
    "homeassistant.core": ha_core,
    "homeassistant.helpers": ha_helpers,
    "homeassistant.helpers.update_coordinator": ha_uc,
    "homeassistant.util": ha_util,
    "homeassistant.util.dt": ha_dt,
    "homeassistant.config_entries": ha_config_entries,
}.items():
    sys.modules[name] = mod

# Register a synthetic `octopus_battery` package (with __path__ set) so the
# relative imports in homeassistant_rates.py resolve without executing the
# real package __init__.py (which pulls in voluptuous, aiohttp, ...).
pkg = types.ModuleType("octopus_battery")
pkg.__path__ = ["./octopus_battery"]
sys.modules["octopus_battery"] = pkg

spec = importlib.util.spec_from_file_location(
    "octopus_battery.homeassistant_rates",
    "./octopus_battery/homeassistant_rates.py",
)
mod = importlib.util.module_from_spec(spec)
sys.modules["octopus_battery.homeassistant_rates"] = mod
spec.loader.exec_module(mod)
HomeAssistantRatesCoordinator = mod.HomeAssistantRatesCoordinator

spec = importlib.util.spec_from_file_location(
    "octopus_battery.const", "./octopus_battery/const.py"
)
const = importlib.util.module_from_spec(spec)
sys.modules["octopus_battery.const"] = const
spec.loader.exec_module(const)

# ---------------------------------------------------------------------------
# Run a real event loop on a background thread (like HA does)
# ---------------------------------------------------------------------------
loop = asyncio.new_event_loop()
loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
loop_thread.start()


class FakeHass:
    """Mimics the real HomeAssistant object, incl. the thread-safety check."""

    def __init__(self, loop):
        self.loop = loop

    def async_create_task(self, coro, name=None):
        # HA raises when this is called off the event loop thread.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "calls hass.async_create_task from a thread other than the "
                "event loop"
            ) from None
        if running is not self.loop:
            raise RuntimeError("not the HA event loop")
        return asyncio.Task(coro, loop=self.loop)


class FakeEntry:
    data = {
        const.CONF_PRICE_ENTITY: "event.octopus_energy_x_y_current_day_rates",
        const.CONF_USE_HOURS: 4,
        const.CONF_CHARGE_HOURS: 4,
    }
    options = {}


hass = FakeHass(loop)
coord = HomeAssistantRatesCoordinator(hass, FakeEntry())
coord._price_entity = FakeEntry.data[const.CONF_PRICE_ENTITY]
coord._mpan = "MPAN123"

now = datetime.now(timezone.utc)
day_start = (now - timedelta(days=1)).replace(
    hour=0, minute=0, second=0, microsecond=0
)
fmt = "%Y-%m-%dT%H:%M:%S+00:00"
rates = [
    {
        "start": day_start.strftime(fmt),
        "end": (day_start + timedelta(hours=1)).strftime(fmt),
        "value_inc_vat": 0.1234,
    },
    {
        "start": (day_start + timedelta(hours=1)).strftime(fmt),
        "end": (day_start + timedelta(hours=2)).strftime(fmt),
        "value_inc_vat": 0.5678,
    },
]
event = Event(
    const.EVENT_ELECTRICITY_CURRENT_DAY_RATES,
    {"rates": rates, "mpan": "MPAN123", "tariff_code": "E-1R-AGILE-24-10-01-A"},
)

failures = []

# 1. The reported failure mode: listener invoked from a worker thread.
with ThreadPoolExecutor(max_workers=1) as ex:
    fut = ex.submit(coord._handle_rate_event, event)
    fut.result(timeout=5)  # must not raise

for _ in range(100):
    if coord.data is not None:
        break
    time.sleep(0.05)
if coord.data is None:
    failures.append("worker-thread event: rates never published on the loop")
elif len(coord._rates) != 2:
    failures.append(
        f"worker-thread event: expected 2 cached rates, got {len(coord._rates)}"
    )
elif abs(coord._rates[list(coord._rates)[0]].value - 12.34) > 1e-6:
    failures.append("worker-thread event: value not scaled to p/kWh")

# 2. Normal path: event fired on the loop (direct synchronous call).
async def call_on_loop():
    coord._handle_rate_event(event)  # invoked *on* the loop thread


asyncio.run_coroutine_threadsafe(call_on_loop(), loop).result(timeout=5)
time.sleep(0.2)
if len(coord._rates) != 2:
    failures.append(
        f"loop-thread event: expected 2 cached rates, got {len(coord._rates)}"
    )

# 3. Events for other meters are ignored.
other = Event(
    const.EVENT_ELECTRICITY_CURRENT_DAY_RATES,
    {"rates": rates, "mpan": "SOMEBODY_ELSE", "tariff_code": "X"},
)
with ThreadPoolExecutor(max_workers=1) as ex:
    ex.submit(coord._handle_rate_event, other).result(timeout=5)
time.sleep(0.2)
if len(coord._rates) != 2:
    failures.append(f"foreign-meter event changed the cache: {len(coord._rates)}")

# 4. Shut-down guard: no loop -> no crash.
hass_noloop = FakeHass(None)
coord2 = HomeAssistantRatesCoordinator(hass_noloop, FakeEntry())
coord2._mpan = "MPAN123"
try:
    with ThreadPoolExecutor(max_workers=1) as ex:
        ex.submit(coord2._handle_rate_event, event).result(timeout=5)
except Exception as e:
    failures.append(f"no-loop guard raised: {e!r}")

loop.call_soon_threadsafe(loop.stop)
loop_thread.join(timeout=5)
loop.close()
if failures:
    print("FAIL")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("OK: worker-thread events handled via loop hop; on-loop path unchanged")
print("OK: foreign-meter filtering and no-loop guard work")
