"""Constants for the Octopus Battery Optimizer integration."""

DOMAIN = "octopus_battery"

# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------
CONF_NAME = "name"
CONF_BATTERY_SWITCH = "battery_switch"
CONF_SHELLY_SWITCH = "shelly_switch"
CONF_SOC_SENSOR = "soc_sensor"
CONF_USE_HOURS = "use_hours"
CONF_CHARGE_HOURS = "charge_hours"
CONF_PRODUCT_CODE = "product_code"
CONF_TARIFF_CODE = "tariff_code"
CONF_DISCHARGE_STOP_SOC = "discharge_stop_soc"
CONF_TOPUP_TRIGGER_SOC = "topup_trigger_soc"
CONF_TOPUP_TARGET_SOC = "topup_target_soc"
CONF_CHARGE_TARGET_SOC = "charge_target_soc"
CONF_CHECK_INTERVAL = "check_interval"
CONF_DRY_RUN = "dry_run"
# Where the electricity prices come from (see PRICE_SOURCE_*).
CONF_PRICE_SOURCE = "price_source"
# The "current day rates" event entity from the BottlecapDave "octopus_energy"
# integration (e.g. ``event.octopus_energy_<serial>_<mpan>_current_day_rates``).
# Only used when ``price_source`` is ``homeassistant``.
CONF_PRICE_ENTITY = "price_entity"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_NAME = "Octopus Battery"
DEFAULT_USE_HOURS = 4
DEFAULT_CHARGE_HOURS = 4
DEFAULT_PRODUCT_CODE = "AGILE-24-10-01"
DEFAULT_TARIFF_CODE = "E-1R-AGILE-24-10-01-A"
DEFAULT_DISCHARGE_STOP_SOC = 5
DEFAULT_TOPUP_TRIGGER_SOC = 3
DEFAULT_TOPUP_TARGET_SOC = 5
DEFAULT_CHARGE_TARGET_SOC = 100
DEFAULT_CHECK_INTERVAL = 5  # minutes
DEFAULT_DRY_RUN = False

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MIN_HOURS = 1
MAX_HOURS = 23
MIN_SOC = 0
MAX_SOC = 100
MIN_CHECK_INTERVAL = 1
MAX_CHECK_INTERVAL = 60

# ---------------------------------------------------------------------------
# Price source
# ---------------------------------------------------------------------------
# "api" polls the public Octopus API (the original behaviour). "homeassistant"
# reads the prices published by the BottlecapDave "octopus_energy" Home
# Assistant integration instead, so no direct API polling is needed here.
PRICE_SOURCE_API = "api"
PRICE_SOURCE_HOMEASSISTANT = "homeassistant"
VALID_PRICE_SOURCES = (PRICE_SOURCE_API, PRICE_SOURCE_HOMEASSISTANT)
# Default price source: poll the Octopus API directly (the original behaviour).
DEFAULT_PRICE_SOURCE = PRICE_SOURCE_API

# Event types fired by the "octopus_energy" integration when a day's
# electricity rates are (re)published. Kept in sync with the source
# integration's ``const.py``.
EVENT_ELECTRICITY_CURRENT_DAY_RATES = "octopus_energy_electricity_current_day_rates"
EVENT_ELECTRICITY_NEXT_DAY_RATES = "octopus_energy_electricity_next_day_rates"
EVENT_ELECTRICITY_PREVIOUS_DAY_RATES = "octopus_energy_electricity_previous_day_rates"
EVENT_ELECTRICITY_DAY_RATES = (
    EVENT_ELECTRICITY_CURRENT_DAY_RATES,
    EVENT_ELECTRICITY_NEXT_DAY_RATES,
    EVENT_ELECTRICITY_PREVIOUS_DAY_RATES,
)
# How often to re-read the rate event entities as a fallback (the events are the
# primary, immediate mechanism). 30 min comfortably tracks the source's ~15 min
# rate refresh while keeping load minimal.
RATE_POLL_MINUTES = 30

# ---------------------------------------------------------------------------
# Controller modes
# ---------------------------------------------------------------------------
MODE_IDLE_NOT_CHARGING = "idle_not_charging"  # drained/used, resting, awaiting its charge block
MODE_IDLE_FLOATING = "idle_floating"          # at charge target, floating at 100% until next discharge
MODE_CHARGING = "charging"
MODE_DISCHARGING = "discharging"
MODE_TOPUP = "top_up"

VALID_MODES = (
    MODE_IDLE_NOT_CHARGING,
    MODE_IDLE_FLOATING,
    MODE_CHARGING,
    MODE_DISCHARGING,
    MODE_TOPUP,
)

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
SERVICE_SET_MODE = "set_mode"
SERVICE_CLEAR_OVERRIDE = "clear_override"
ATTR_MODE = "mode"
ATTR_ENTRY_ID = "entry_id"

# ---------------------------------------------------------------------------
# Octopus API
# ---------------------------------------------------------------------------
OCTOPUS_API_BASE = "https://api.octopus.energy/v1"
OCTOPUS_API_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Platforms / storage
# ---------------------------------------------------------------------------
PLATFORMS = ["sensor", "switch"]
