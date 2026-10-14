"""CSV upload and progress dialog for importing fueling history."""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from .const import CONF_VEHICLE_TYPE, DOMAIN
from .fueling import MAX_CSV_BYTES, CSVImportError, parse_csv

_LOGGER = logging.getLogger(__name__)


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
        if coordinator.fueling_history.load_error is not None:
            return self.async_abort(reason="history_unavailable")
        if "recorder" not in self.hass.config.components:
            return self.async_abort(reason="recorder_not_loaded")
        await async_setup_component(self.hass, "file_upload", {})
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
        from homeassistant.components import persistent_notification
        from sqlalchemy.exc import SQLAlchemyError

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
        from homeassistant.helpers.selector import FileSelector, FileSelectorConfig

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
        from homeassistant.components.file_upload import process_uploaded_file

        with process_uploaded_file(self.hass, file_id) as path, path.open("rb") as file:
            content = file.read(MAX_CSV_BYTES + 1)
        return parse_csv(content, self.config_entry.data.get(CONF_VEHICLE_TYPE))
