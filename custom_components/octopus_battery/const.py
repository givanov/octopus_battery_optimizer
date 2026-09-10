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
CONF_READ_ONLY = "read_only"

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
DEFAULT_READ_ONLY = False

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
# Controller modes
# ---------------------------------------------------------------------------
MODE_IDLE = "idle"
MODE_CHARGING = "charging"
MODE_DISCHARGING = "discharging"
MODE_TOPUP = "top_up"

VALID_MODES = (MODE_IDLE, MODE_CHARGING, MODE_DISCHARGING, MODE_TOPUP)

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
