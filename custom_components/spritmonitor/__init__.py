# Contenido para: __init__.py

import logging
import aiohttp
from datetime import timedelta, datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    API_VEHICLES_URL,
    API_REMINDERS_URL,
    API_FUELINGS_URL_TPL,
    CONF_VEHICLE_ID,
    CONF_APP_TOKEN,
    CONF_BEARER_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    CONF_VEHICLE_TYPE,
    VEHICLE_TYPE_ELECTRIC
)

from .services import async_setup_services
from .fueling import data_with_fuelings
from .history import FuelingHistory, tank_totals

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Integration setup."""
    async_setup_services(hass)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Spritmonitor from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    vehicle_id = entry.data[CONF_VEHICLE_ID]
    app_token = entry.data[CONF_APP_TOKEN]
    bearer_token = entry.data[CONF_BEARER_TOKEN]
    vehicle_type = entry.data.get(CONF_VEHICLE_TYPE)
    update_interval_hours = entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
    
    headers = {
        "Accept": "application/json",
        "Application-Id": app_token,
        "Authorization": bearer_token
    }

    session = async_get_clientsession(hass)
    history = FuelingHistory(hass, entry)
    await history.async_load()

    async def async_update_data():
        """Fetch and process data from the API endpoint."""
        try:
            async with session.get(API_VEHICLES_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                response.raise_for_status()
                vehicles = await response.json()
                vehicle_info = next((v for v in vehicles if v["id"] == vehicle_id), None)
                if not vehicle_info:
                    raise UpdateFailed(f"Vehicle with ID {vehicle_id} not found")
            
            trip_unit = vehicle_info.get("tripunit")
            if vehicle_type == VEHICLE_TYPE_ELECTRIC:
                quantity_unit = UnitOfEnergy.KILO_WATT_HOUR
            else:
                quantity_unit = "L" if trip_unit == "km" else "gal"
            quantity_unit = vehicle_info.get("quantityunit", quantity_unit)
            consumption_unit_raw = vehicle_info.get("consumptionunit", "")
            consumption_unit = consumption_unit_raw.replace('km/l', 'km/L').replace('l/100km', 'L/100km')
            units = {"trip": trip_unit, "quantity": quantity_unit, "consumption": consumption_unit}

            fuelings_url = API_FUELINGS_URL_TPL.format(vehicle_id=vehicle_id)
            async with session.get(f"{fuelings_url}?limit=20", headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                response.raise_for_status()
                all_fuelings = await response.json()

            all_fuelings = await history.async_merge_api(all_fuelings)

            reminders = None
            try:
                async with session.get(API_REMINDERS_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 200:
                        all_reminders = await response.json()
                        reminders = [r for r in all_reminders if r.get('vehicle') == vehicle_id]
            except Exception as e:
                _LOGGER.debug("Could not fetch reminders: %s", e)

            data = data_with_fuelings(
                {"vehicle": vehicle_info, "units": units, "reminders": reminders},
                all_fuelings,
                vehicle_type,
            )
            if history.imported:
                data["tank_totals"] = tank_totals(all_fuelings)
            return data
        except aiohttp.ClientError as e:
            raise UpdateFailed(f"Connection error with Spritmonitor: {e}")
        except Exception as e:
            _LOGGER.exception("Unexpected error fetching Spritmonitor data")
            raise UpdateFailed(f"Error fetching data from Spritmonitor: {e}")

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name=f"spritmonitor_{vehicle_id}",
        update_method=async_update_data,
        update_interval=timedelta(hours=update_interval_hours),
        config_entry=entry,
    )
    coordinator.fueling_history = history

    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok