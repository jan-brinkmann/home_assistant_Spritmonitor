# Contenido para: config_flow.py

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol
import aiohttp
import logging
from homeassistant.components import persistent_notification
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import FileSelector, FileSelectorConfig
from sqlalchemy.exc import SQLAlchemyError

from .const import (
    DOMAIN,
    DEFAULT_APP_TOKEN,
    DEFAULT_UPDATE_INTERVAL,
    API_VEHICLES_URL,
    CONF_VEHICLE_ID,
    CONF_APP_TOKEN,
    CONF_BEARER_TOKEN,
    CONF_UPDATE_INTERVAL,
    CONF_VEHICLE_TYPE,
    CONF_CURRENCY,
    VEHICLE_TYPE_COMBUSTION,
    VEHICLE_TYPE_ELECTRIC,
    VEHICLE_TYPE_PHEV,
)
from .fueling import MAX_CSV_BYTES, CSVImportError, parse_csv

_LOGGER = logging.getLogger(__name__)

class SpritmonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    _attr_translation_domain = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SpritmonitorOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        
        if user_input is not None:
            try:
                session = async_get_clientsession(self.hass)
                vehicle_info = await self._get_vehicle_info(
                    session, user_input[CONF_VEHICLE_ID], user_input[CONF_APP_TOKEN], user_input[CONF_BEARER_TOKEN]
                )
                if vehicle_info:
                    make = vehicle_info.get("make", "")
                    model = vehicle_info.get("model", "")
                    title = f"{make} {model}".strip() if make and model else f"Spritmonitor Vehicle {user_input[CONF_VEHICLE_ID]}"
                    
                    await self.async_set_unique_id(f"spritmonitor_{user_input[CONF_VEHICLE_ID]}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(title=title, data=user_input)
                else:
                    errors["base"] = "invalid_auth"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            except Exception as e:
                _LOGGER.error("Error during configuration: %s", e)
                errors["base"] = "cannot_connect"

        # --- DATA_SCHEMA SIMPLIFICADO ---
        data_schema = vol.Schema({
            vol.Required(CONF_VEHICLE_ID): int,
            vol.Required(CONF_APP_TOKEN, default=DEFAULT_APP_TOKEN): str,
            vol.Required(CONF_BEARER_TOKEN): str,
            vol.Required(CONF_VEHICLE_TYPE, default=VEHICLE_TYPE_COMBUSTION): vol.In([
                VEHICLE_TYPE_COMBUSTION, 
                VEHICLE_TYPE_ELECTRIC,
                VEHICLE_TYPE_PHEV,
            ]),
            vol.Required(CONF_CURRENCY, default="USD"): str,
            vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=24)
            )
        })

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def _get_vehicle_info(self, session: aiohttp.ClientSession, vehicle_id: int, app_token: str, bearer_token: str) -> dict | None:
        headers = { "Application-Id": app_token, "Authorization": bearer_token }
        async with session.get(API_VEHICLES_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            vehicles = await response.json()
            return next((v for v in vehicles if v["id"] == vehicle_id), None)


class SpritmonitorOptionsFlow(config_entries.OptionsFlow):
    """Upload the vehicle's export through the standard Home Assistant GUI."""

    def __init__(self):
        self._import_task = None

    async def async_step_init(self, user_input=None):
        errors = {}
        detail = ""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="integration_not_loaded")
        if user_input is not None:
            try:
                records = await self.hass.async_add_executor_job(
                    self._read_upload, user_input["csv_file"]
                )
                coordinator.fueling_history.validate_units(coordinator, records)
            except (CSVImportError, ValueError, OSError) as err:
                errors["base"] = "invalid_csv"
                detail = str(err)
            else:
                self._import_task = self.hass.async_create_background_task(
                    coordinator.fueling_history.async_import(coordinator, records),
                    f"Spritmonitor CSV {self.config_entry.entry_id}",
                )
                return await self.async_step_import()
        return self._show_upload_form(errors, detail)

    async def async_step_import(self, user_input=None):
        if self._import_task.done():
            return self.async_show_progress_done(next_step_id="finish")
        return self.async_show_progress(
            step_id="import",
            progress_action="import",
            progress_task=self._import_task,
        )

    async def async_step_finish(self, user_input=None):
        try:
            result = self._import_task.result()
        except CSVImportError as err:
            return self._show_upload_form({"base": "invalid_csv"}, str(err))
        except (HomeAssistantError, OSError, ValueError, SQLAlchemyError) as err:
            _LOGGER.exception("CSV statistics import failed")
            return self._show_upload_form({"base": "import_failed"}, str(err))
        persistent_notification.async_create(
            self.hass,
            f"{result['added']} neue Tankvorgänge; {result['duplicates']} bereits bekannte "
            f"Tankvorgänge zusammengeführt. {result['total']} Tankvorgänge insgesamt. "
            f"Die Langzeitstatistik von {result['sensors']} vorhandenen Sensoren wurde geschrieben.",
            title=f"Spritmonitor CSV: {self.config_entry.title}",
            notification_id=f"spritmonitor_csv_{self.config_entry.entry_id}",
        )
        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    def _show_upload_form(self, errors, detail):
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("csv_file"): FileSelector(
                        FileSelectorConfig(accept=".csv,text/csv")
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"error": detail},
        )

    def _read_upload(self, file_id):
        """Read and dispose of the upload entirely in the executor thread."""
        with process_uploaded_file(self.hass, file_id) as path, path.open("rb") as file:
            content = file.read(MAX_CSV_BYTES + 1)
        return parse_csv(content, self.config_entry.data[CONF_VEHICLE_TYPE])
