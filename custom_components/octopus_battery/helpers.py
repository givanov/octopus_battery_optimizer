"""Small shared helpers for the Octopus Battery Optimizer integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry


def effective_data(entry: ConfigEntry) -> dict[str, Any]:
    """Return the entry data merged with its options (options win).

    The options flow updates ``entry.options``; the controller and
    coordinator always read through this helper so option changes take
    effect (a config-entry reload re-creates both objects anyway).
    """
    return {**entry.data, **entry.options}
