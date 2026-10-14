"""Persistent fueling ledger and backfill of the existing entities' statistics."""

import asyncio
import math
from bisect import bisect_right
from datetime import timedelta
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import StatisticsShortTerm
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_import_statistics,
    get_metadata,
    get_start_time,
    statistics_during_period,
)
from homeassistant.components.sensor import SensorStateClass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import CONF_CURRENCY, CONF_VEHICLE_TYPE, DOMAIN
from .fueling import (
    CSVImportError,
    data_with_fuelings,
    fueling_date,
    fueling_reset,
    fueling_sort_key,
    merge_fuelings,
)

HISTORICAL_SENSORS = {
    "total_distance",
    "last_refuel_odometer",
    "last_refuel_trip",
    "last_refuel_cost",
    "total_fuel",
    "avg_consumption",
    "last_refuel_quantity",
    "last_refuel_price_per_liter",
    "last_refuel_consumption",
    "total_energy_charged",
    "avg_energy_consumption",
    "last_charge_energy",
    "last_charge_price_per_kwh",
    "last_charge_consumption",
    "efficiency_per_distance",
    "monthly_energy_charged",
}
CALCULATED_PREFIXES = (
    "consumption_consistency",
    "avg_refuel_quantity",
    "avg_days_between_refuels",
    "price_variability",
    "eco_driving_index",
    "cost_per_distance",
)


def tank_totals(records):
    """Cumulative totals; first fills establish the average consumption baseline."""
    result = {}
    for tank in (1, 2):
        valid = [
            r for r in records if r["tankid"] == tank and r.get("type") != "invalid"
        ]
        quantity = sum(float(r.get("quantity") or 0) for r in valid)
        trip = sum(float(r.get("trip") or 0) for r in valid)
        consumed = sum(
            float(r.get("quantity") or 0) for r in valid if r.get("type") != "first"
        )
        result[tank] = {
            "quantity": quantity,
            "trip": trip,
            "consumption": consumed * 100 / trip if trip else 0,
        }
    return result


def historical_snapshots(data, records, vehicle_type, timezone):
    """Replay only data known at each fueling, never today's vehicle aggregates."""
    snapshots = []
    prefix = []
    for record in sorted(records, key=fueling_sort_key):
        prefix.append(record)
        totals = tank_totals(prefix)
        primary = totals[2 if vehicle_type == "electric" else 1]
        consumption = primary["consumption"]
        if data["units"].get("consumption") in {"km/L", "km/l"}:
            consumption = 100 / consumption if consumption else 0
        snapshot = data_with_fuelings(data, prefix, vehicle_type)
        snapshot["vehicle"] = {
            **data["vehicle"],
            "tripsum": primary["trip"],
            "quantitysum": primary["quantity"],
            "consumption": consumption,
        }
        snapshot["tank_totals"] = totals
        snapshot["reminders"] = []
        snapshot["reference_date"] = fueling_date(record["date"])
        snapshot["event_tank"] = record["tankid"]
        start = dt_util.as_utc(fueling_date(record["date"]).replace(tzinfo=timezone))
        start = start.replace(minute=0, second=0, microsecond=0)
        snapshots.append((start, snapshot))
    return snapshots


def _sensor_value(sensor, data):
    try:
        value = sensor._value_fn(data)
        return (
            float(value) if value is not None and math.isfinite(float(value)) else None
        )
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        return None


def build_statistics(sensor, snapshots, timezone, convert):
    """Group same-day events in one hour, keeping their range and cumulative sums."""
    rows = {}
    observations = {}
    first_odometer = None
    for start, data in snapshots:
        key = sensor.sensor_id
        electric = key in {
            "total_energy_charged",
            "avg_energy_consumption",
            "last_charge_energy",
            "last_charge_price_per_kwh",
            "last_charge_consumption",
            "monthly_energy_charged",
            "efficiency_per_distance",
        } or key.endswith("_electric")
        primary_tank = (
            2
            if sensor.coordinator.config_entry.data[CONF_VEHICLE_TYPE] == "electric"
            else 1
        )
        if data["event_tank"] != (2 if electric else primary_tank):
            continue
        value = _sensor_value(sensor, data)
        if value is None:
            continue
        value = convert(value)
        row = {"start": start}
        if sensor.state_class == SensorStateClass.MEASUREMENT:
            values = observations.setdefault(start, [])
            values.append(value)
            row.update(mean=sum(values) / len(values), min=min(values), max=max(values))
        else:
            row["state"] = value
            if key == "last_refuel_odometer":
                if first_odometer is None:
                    first_odometer = value
                row["sum"] = value - first_odometer
            elif key == "last_refuel_cost":
                row["sum"] = convert(
                    sum(float(r.get("cost") or 0) for r in data["refuelings"])
                )
                row["last_reset"] = dt_util.as_utc(
                    fueling_reset(data["last_refueling"], timezone)
                )
            elif key == "monthly_energy_charged":
                row["sum"] = convert(data["tank_totals"][2]["quantity"])
                row["last_reset"] = dt_util.as_utc(
                    data["reference_date"].replace(day=1, tzinfo=timezone)
                )
            else:
                row["sum"] = value
        rows[start] = row
    return [rows[start] for start in sorted(rows)]


def reconcile_existing(imported, existing, *, short_term=False):
    """Replace overlaps and set the baseline for future automatic statistics.

    Recalculate sums from states, so repeating an import cannot add an offset
    twice. Keep subsequent live observations instead of deleting their history.
    """
    starts = [row["start"].timestamp() for row in imported]
    result = {}
    for old in existing:
        timestamp = old["start"]
        index = bisect_right(starts, timestamp) - 1
        if index < 0:
            continue
        baseline = imported[index]
        start = dt_util.utc_from_timestamp(timestamp)
        if timestamp <= starts[-1]:
            row = {**baseline, "start": start}
        else:
            row = {
                key: old[key]
                for key in ("mean", "min", "max", "state", "sum")
                if old.get(key) is not None
            }
            row["start"] = start
            if "sum" in baseline:
                delta = float(row.get("state", baseline["state"])) - baseline["state"]
                row["sum"] = baseline["sum"] + (
                    delta if "last_reset" not in baseline else 0
                )
                if "last_reset" in baseline:
                    row["last_reset"] = baseline["last_reset"]
        result[start] = row
    if not short_term:
        result.update({row["start"]: row for row in imported})
    return [result[start] for start in sorted(result)]


class FuelingHistory:
    """Vehicle-scoped persisted history shared by polling and the GUI import."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.vehicle_type = entry.data[CONF_VEHICLE_TYPE]
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.fuelings")
        self.records = []
        self.imported = False
        self.lock = asyncio.Lock()
        self.sensors = {}

    async def async_load(self):
        if saved := await self.store.async_load():
            self.records = saved.get("records", [])
            self.imported = saved.get("imported", False)

    async def _async_save(self):
        await self.store.async_save(
            {"records": self.records, "imported": self.imported}
        )

    async def async_merge_api(self, incoming):
        async with self.lock:
            merged, _, _ = merge_fuelings(self.records, incoming, self.vehicle_type)
            if merged != self.records:
                self.records = merged
                await self._async_save()
            return list(self.records)

    def validate_units(self, coordinator, records):
        units = coordinator.data["units"]
        if units.get("trip") != "km":
            raise CSVImportError(
                "Dieser CSV-Import erwartet Kilometer. Das Fahrzeug muss in Spritmonitor km verwenden."
            )
        if self.vehicle_type != "electric" and units.get("quantity") not in {"L", "l"}:
            raise CSVImportError(
                "Dieser CSV-Import erwartet Liter für den Kraftstofftank."
            )
        currency = self.entry.data.get(CONF_CURRENCY, "")
        currency = {"€": "EUR", "$": "USD", "£": "GBP"}.get(currency, currency).upper()
        if any(
            r.get("currency") and r["currency"].upper() != currency for r in records
        ):
            raise CSVImportError(
                "Die CSV-Währung muss zur konfigurierten Sensor-Währung passen; eine Umrechnung findet nicht statt."
            )
        if any(fueling_date(r["date"]).date() > dt_util.now().date() for r in records):
            raise CSVImportError(
                "Die CSV enthält Tankvorgänge mit einem Datum in der Zukunft."
            )

    async def async_import(self, coordinator, records):
        self.validate_units(coordinator, records)
        async with self.lock:
            merged, added, duplicates = merge_fuelings(
                self.records, records, self.vehicle_type
            )
            data = data_with_fuelings(coordinator.data, merged, self.vehicle_type)
            data["tank_totals"] = tank_totals(merged)
            series = await self._async_prepare_statistics(data, merged)
            if not series:
                raise CSVImportError(
                    "Keine aktiven vorhandenen Sensoren für den Statistikimport gefunden."
                )
            self.records = merged
            self.imported = True
            # Persist the source first, so an interrupted import can be retried.
            await self._async_save()
            coordinator.async_set_updated_data(data)
            recorder = get_instance(self.hass)
            for metadata, hourly, short in series:
                async_import_statistics(self.hass, metadata, hourly)
                if short:
                    recorder.async_import_statistics(
                        metadata, short, StatisticsShortTerm
                    )
            await recorder.async_block_till_done()
            await recorder.async_add_executor_job(self._verify_statistics, series)
            return {
                "added": added,
                "duplicates": duplicates,
                "total": len(merged),
                "sensors": len(series),
            }

    def _verify_statistics(self, series):
        """Don't report success if Recorder failed or deferred a database write."""
        for metadata, hourly, short in series:
            statistic_id = metadata["statistic_id"]
            units = {metadata["unit_class"]: metadata["unit_of_measurement"]}
            checks = [(hourly[0], "hour", timedelta(hours=1))]
            if short:
                checks.append((short[-1], "5minute", timedelta(minutes=5)))
            for expected, period, duration in checks:
                saved = statistics_during_period(
                    self.hass,
                    expected["start"],
                    expected["start"] + duration,
                    {statistic_id},
                    period,
                    units,
                    {"state", "sum", "mean", "min", "max"},
                ).get(statistic_id, [])
                if not saved or any(
                    saved[0].get(key) is None
                    or not math.isclose(
                        saved[0][key], value, rel_tol=1e-9, abs_tol=1e-9
                    )
                    for key, value in expected.items()
                    if key in {"state", "sum", "mean", "min", "max"}
                ):
                    raise HomeAssistantError(
                        f"Recorder statistics were not written for {statistic_id}. Retry the CSV import."
                    )

    async def _async_prepare_statistics(self, data, records):
        recorder = get_instance(self.hass)
        if not await recorder.async_db_ready:
            raise HomeAssistantError("Recorder database is unavailable")
        await recorder.async_block_till_done()
        sensors = [
            sensor
            for sensor in self.sensors.values()
            if sensor.entity_id and sensor.hass is not None
        ]
        sensors = [
            s
            for s in sensors
            if s.sensor_id in HISTORICAL_SENSORS
            or s.sensor_id.startswith(CALCULATED_PREFIXES)
        ]
        ids = {s.entity_id for s in sensors}
        timezone = dt_util.DEFAULT_TIME_ZONE
        snapshots = await self.hass.async_add_executor_job(
            historical_snapshots, data, records, self.vehicle_type, timezone
        )
        metadata = await recorder.async_add_executor_job(
            partial(get_metadata, self.hass, statistic_ids=ids)
        )
        first = snapshots[0][0]
        types = {"mean", "min", "max", "state", "sum"}
        hourly = await recorder.async_add_executor_job(
            statistics_during_period, self.hass, first, None, ids, "hour", None, types
        )
        short = await recorder.async_add_executor_job(
            statistics_during_period,
            self.hass,
            first,
            None,
            ids,
            "5minute",
            None,
            types,
        )
        series = []
        for sensor in sensors:
            unit = sensor.native_unit_of_measurement
            old_meta = metadata.get(sensor.entity_id)
            state = self.hass.states.get(sensor.entity_id)
            target_unit = (
                old_meta[1]["unit_of_measurement"]
                if old_meta
                else (
                    state.attributes.get("unit_of_measurement", unit) if state else unit
                )
            )
            converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
            if unit != target_unit and (
                converter is None or target_unit not in converter.VALID_UNITS
            ):
                raise CSVImportError(
                    f"Nicht kompatible Statistik-Einheit für {sensor.entity_id}: {target_unit}."
                )
            convert = (
                partial(converter.convert, from_unit=unit, to_unit=target_unit)
                if unit != target_unit
                else float
            )
            imported = await self.hass.async_add_executor_job(
                build_statistics, sensor, snapshots, timezone, convert
            )
            if not imported:
                continue
            meta = (
                dict(old_meta[1])
                if old_meta
                else {
                    "name": None,
                    "source": "recorder",
                    "statistic_id": sensor.entity_id,
                    "unit_of_measurement": target_unit,
                }
            )
            measurement = sensor.state_class == SensorStateClass.MEASUREMENT
            meta["mean_type"] = (
                StatisticMeanType.ARITHMETIC if measurement else StatisticMeanType.NONE
            )
            meta["has_sum"] = not measurement
            meta["unit_class"] = converter.UNIT_CLASS if converter else None
            old_hourly = hourly.get(sensor.entity_id, [])
            old_short = short.get(sensor.entity_id, [])
            if (
                old_meta
                and state
                and state.attributes.get("unit_of_measurement") != target_unit
                and converter
            ):
                # The query normally converts to the entity's display unit.
                # Explicitly request the database unit before rewriting rows.
                requested_units = {converter.UNIT_CLASS: target_unit}
                old_hourly = (
                    await recorder.async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        first,
                        None,
                        {sensor.entity_id},
                        "hour",
                        requested_units,
                        types,
                    )
                ).get(sensor.entity_id, [])
                old_short = (
                    await recorder.async_add_executor_job(
                        statistics_during_period,
                        self.hass,
                        first,
                        None,
                        {sensor.entity_id},
                        "5minute",
                        requested_units,
                        types,
                    )
                ).get(sensor.entity_id, [])
            hour_rows = await self.hass.async_add_executor_job(
                reconcile_existing, imported, old_hourly
            )
            short_rows = await self.hass.async_add_executor_job(
                partial(reconcile_existing, short_term=True), imported, old_short
            )
            if not measurement:
                # Recorder continues from short-term statistics, not hourly rows.
                # Seed a recent baseline even when this entity had none yet.
                bridge = dict(imported[-1])
                bridge["start"] = get_start_time()
                current = _sensor_value(sensor, data)
                if current is not None:
                    current = convert(current)
                    if "last_reset" not in bridge:
                        bridge["sum"] += current - bridge["state"]
                    bridge["state"] = current
                if sensor.sensor_id == "monthly_energy_charged":
                    bridge["last_reset"] = dt_util.as_utc(
                        dt_util.now().replace(
                            day=1, hour=0, minute=0, second=0, microsecond=0
                        )
                    )
                short_rows = [
                    row for row in short_rows if row["start"] != bridge["start"]
                ]
                short_rows.append(bridge)
                short_rows.sort(key=lambda row: row["start"])
            series.append((meta, hour_rows, short_rows))
        return series
