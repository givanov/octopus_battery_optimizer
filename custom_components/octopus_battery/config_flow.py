"""Config flow for the Octopus Battery Optimizer integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_SWITCH,
    CONF_CHARGE_HOURS,
    CONF_CHARGE_TARGET_SOC,
    CONF_CHECK_INTERVAL,
    CONF_DISCHARGE_STOP_SOC,
    CONF_NAME,
    CONF_PRODUCT_CODE,
    CONF_DRY_RUN,
    CONF_SHELLY_SWITCH,
    CONF_SOC_SENSOR,
    CONF_TARIFF_CODE,
    CONF_TOPUP_TARGET_SOC,
    CONF_TOPUP_TRIGGER_SOC,
    CONF_USE_HOURS,
    DEFAULT_CHARGE_HOURS,
    DEFAULT_CHARGE_TARGET_SOC,
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_DISCHARGE_STOP_SOC,
    DEFAULT_NAME,
    DEFAULT_PRODUCT_CODE,
    DEFAULT_DRY_RUN,
    DEFAULT_TARIFF_CODE,
    DEFAULT_TOPUP_TARGET_SOC,
    DEFAULT_TOPUP_TRIGGER_SOC,
    DEFAULT_USE_HOURS,
    DOMAIN,
    MAX_CHECK_INTERVAL,
    MAX_HOURS,
    MAX_SOC,
    MIN_CHECK_INTERVAL,
    MIN_HOURS,
    MIN_SOC,
)
from .helpers import effective_data

_LOGGER = logging.getLogger(__name__)

ENTITY_SWITCH = selector.EntitySelector({"filter": {"domain": "switch"}})
ENTITY_SENSOR = selector.EntitySelector({"filter": {"domain": "sensor"}})


def _settings_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the settings form schema with per-field defaults.

    Shared by the user step (factory defaults) and the options step
    (the entry's current values), so both flows always offer exactly
    the same settings.
    """
    return vol.Schema(
        {
            vol.Required(
                CONF_BATTERY_SWITCH, default=defaults.get(CONF_BATTERY_SWITCH)
            ): ENTITY_SWITCH,
            vol.Required(
                CONF_SHELLY_SWITCH, default=defaults.get(CONF_SHELLY_SWITCH)
            ): ENTITY_SWITCH,
            vol.Required(
                CONF_SOC_SENSOR, default=defaults.get(CONF_SOC_SENSOR)
            ): ENTITY_SENSOR,
            vol.Required(
                CONF_USE_HOURS,
                default=defaults.get(CONF_USE_HOURS, DEFAULT_USE_HOURS),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_HOURS, max=MAX_HOURS)),
            vol.Required(
                CONF_CHARGE_HOURS,
                default=defaults.get(CONF_CHARGE_HOURS, DEFAULT_CHARGE_HOURS),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_HOURS, max=MAX_HOURS)),
            vol.Required(
                CONF_PRODUCT_CODE,
                default=defaults.get(CONF_PRODUCT_CODE, DEFAULT_PRODUCT_CODE),
            ): vol.All(str, vol.Length(min=1, max=32)),
            vol.Required(
                CONF_TARIFF_CODE,
                default=defaults.get(CONF_TARIFF_CODE, DEFAULT_TARIFF_CODE),
            ): vol.All(str, vol.Length(min=1, max=64)),
            vol.Required(
                CONF_DISCHARGE_STOP_SOC,
                default=defaults.get(
                    CONF_DISCHARGE_STOP_SOC, DEFAULT_DISCHARGE_STOP_SOC
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SOC, max=MAX_SOC)),
            vol.Required(
                CONF_TOPUP_TRIGGER_SOC,
                default=defaults.get(
                    CONF_TOPUP_TRIGGER_SOC, DEFAULT_TOPUP_TRIGGER_SOC
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SOC, max=MAX_SOC)),
            vol.Required(
                CONF_TOPUP_TARGET_SOC,
                default=defaults.get(
                    CONF_TOPUP_TARGET_SOC, DEFAULT_TOPUP_TARGET_SOC
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SOC, max=MAX_SOC)),
            vol.Required(
                CONF_CHARGE_TARGET_SOC,
                default=defaults.get(
                    CONF_CHARGE_TARGET_SOC, DEFAULT_CHARGE_TARGET_SOC
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SOC, max=MAX_SOC)),
            vol.Required(
                CONF_CHECK_INTERVAL,
                default=defaults.get(
                    CONF_CHECK_INTERVAL, DEFAULT_CHECK_INTERVAL
                ),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_CHECK_INTERVAL, max=MAX_CHECK_INTERVAL),
            ),
        }
    )


BASE_SCHEMA = _settings_schema({})


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Return a dict of field -> error message (empty when valid)."""
    errors: dict[str, str] = {}

    battery = user_input.get(CONF_BATTERY_SWITCH)
    shelly = user_input.get(CONF_SHELLY_SWITCH)
    soc = user_input.get(CONF_SOC_SENSOR)

    if not battery or not isinstance(battery, str) or "." not in battery:
        errors[CONF_BATTERY_SWITCH] = "required"
    if not shelly or not isinstance(shelly, str) or "." not in shelly:
        errors[CONF_SHELLY_SWITCH] = "required"
    if not soc or not isinstance(soc, str) or "." not in soc:
        errors[CONF_SOC_SENSOR] = "required"
    if battery == shelly:
        errors[CONF_SHELLY_SWITCH] = "same_as_battery"

    use_hours = user_input.get(CONF_USE_HOURS, 0)
    charge_hours = user_input.get(CONF_CHARGE_HOURS, 0)
    if use_hours + charge_hours > 24:
        errors[CONF_CHARGE_HOURS] = "blocks_overlap"

    trigger = user_input.get(CONF_TOPUP_TRIGGER_SOC, 0)
    target = user_input.get(CONF_TOPUP_TARGET_SOC, 100)
    if trigger >= target:
        errors[CONF_TOPUP_TRIGGER_SOC] = "trigger_above_target"

    stop = user_input.get(CONF_DISCHARGE_STOP_SOC, 0)
    charge_target = user_input.get(CONF_CHARGE_TARGET_SOC, 100)
    if stop >= charge_target:
        errors[CONF_DISCHARGE_STOP_SOC] = "stop_above_charge_target"

    return errors


class OctopusBatteryConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Octopus Battery Optimizer."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user configuration step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                entry_title = user_input.get(CONF_NAME) or DEFAULT_NAME
                # A stable unique id so re-adding the same wiring is detected.
                unique_id = "-".join(
                    (
                        user_input[CONF_BATTERY_SWITCH],
                        user_input[CONF_SHELLY_SWITCH],
                        user_input[CONF_SOC_SENSOR],
                    )
                )
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=entry_title, data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): vol.Coerce(str),
                **BASE_SCHEMA.schema,
                # Dry-run is a runtime toggle (driven by the switch entity).
                # It is stored in entry.data and intentionally NOT part of the
                # options flow, so toggling the switch is the single source of
                # truth and a stale options copy can't override it.
                vol.Optional(CONF_DRY_RUN, default=DEFAULT_DRY_RUN): vol.Coerce(bool),
            }
        )
        # Fill in the {charge_hours}/{use_hours} placeholders in the description
        # with the current (or default) values so the frontend can format it.
        current = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "charge_hours": current.get(CONF_CHARGE_HOURS, DEFAULT_CHARGE_HOURS),
                "use_hours": current.get(CONF_USE_HOURS, DEFAULT_USE_HOURS),
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for reconfiguring this entry.

        This is what powers the "Configure" action in
        Settings → Devices & Services → Octopus Battery Optimizer.
        """
        return OctopusBatteryOptionsFlow(config_entry)


class OctopusBatteryOptionsFlow(OptionsFlowWithConfigEntry):
    """Reconfigure an existing entry after initial setup.

    Saved values are written to ``entry.options``; Home Assistant then
    fires the entry's update listener (``_async_update_entry`` in
    ``__init__.py``), which reloads the entry so the coordinator and
    controller pick up the new settings.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the settings of an already-configured entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        # Pre-fill with the currently effective values (entry.data merged
        # with any previously saved options) so the form shows what the
        # integration uses right now.
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(effective_data(self.config_entry)),
            errors=errors,
        )
