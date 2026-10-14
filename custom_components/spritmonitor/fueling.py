"""Parse Spritmonitor exports and reconcile CSV and API fueling records."""

import csv
import io
import math
from datetime import datetime
from decimal import Decimal, InvalidOperation

ELECTRIC_FUELS = {19, 24}
FUELING_TYPES = {0: "invalid", 1: "full", 2: "notfull", 3: "first"}
NUMERIC_COLUMNS = {
    "Km-Stand": "odometer",
    "Teil-Km": "trip",
    "Spritmenge": "quantity",
    "Kosten": "cost",
    "Verbrauch": "consumption",
    "BC-Verbrauch": "bc_consumption",
    "BC-Spritmenge": "bc_quantity",
    "BC-Geschwindigkeit": "bc_speed",
    "Breitengrad": "latitude",
    "Längengrad": "longitude",
}
TEXT_COLUMNS = {
    "Währung": "currency",
    "Bemerkung": "note",
    "Tankstelle": "stationname",
    "Land": "country",
    "Großraum": "region",
    "Ort": "location",
}
MAX_CSV_BYTES = 10 * 1024 * 1024


class CSVImportError(ValueError):
    """An invalid export, with a message suitable for the import dialog."""


def fueling_date(value):
    """Normalize both the export date and API dates."""
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), pattern)
        except ValueError:
            pass
    raise ValueError(f"Invalid fueling date: {value}")


def _number(value, column, line):
    if not value.strip():
        return None
    try:
        number = Decimal(value.strip().replace(",", "."))
        if not number.is_finite():
            raise InvalidOperation
        if column not in {"Breitengrad", "Längengrad"} and number < 0:
            raise InvalidOperation
        result = float(number)
        if not math.isfinite(result):
            raise InvalidOperation
        return result
    except (InvalidOperation, OverflowError) as err:
        raise CSVImportError(f"Zeile {line}: ungültige Zahl in {column}.") from err


def parse_csv(content, vehicle_type):
    """Read a complete export before making any persistent changes."""
    if len(content) > MAX_CSV_BYTES:
        raise CSVImportError("Die CSV darf höchstens 10 MiB groß sein.")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=";", strict=True)
    required = {
        "Datum",
        "Km-Stand",
        "Teil-Km",
        "Spritmenge",
        "Kosten",
        "Währung",
        "Tankart",
        "Kraftstoff",
    }
    try:
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise CSVImportError(
                "Erwartet wird ein Spritmonitor-Export mit Semikolon und deutschen Spaltennamen."
            )
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise CSVImportError("Die CSV enthält doppelte Spaltennamen.")
        records = []
        for row in reader:
            line = reader.line_num
            if None in row or any(value is None for value in row.values()):
                raise CSVImportError(f"Zeile {line}: falsche Anzahl von Spalten.")
            try:
                date = fueling_date(row["Datum"])
                tank_type = int(row["Tankart"])
                fuel = int(row["Kraftstoff"])
                if tank_type not in FUELING_TYPES or fuel <= 0:
                    raise ValueError
            except ValueError as err:
                raise CSVImportError(
                    f"Zeile {line}: ungültiges Datum, Tankart oder Kraftstoff."
                ) from err
            electric = fuel in ELECTRIC_FUELS
            if (vehicle_type == "electric" and not electric) or (
                vehicle_type == "combustion" and electric
            ):
                raise CSVImportError(
                    f"Zeile {line}: Kraftstoff passt nicht zum konfigurierten Fahrzeugtyp."
                )
            record = {
                "date": date.strftime("%d.%m.%Y"),
                "tankid": 2 if electric else 1,
                "fuelsortid": fuel,
                "type": FUELING_TYPES[tank_type],
                "_csv": True,
            }
            for column, key in NUMERIC_COLUMNS.items():
                record[key] = _number(row.get(column, ""), column, line)
            for column, key in TEXT_COLUMNS.items():
                record[key] = row.get(column, "").strip()
            for column, key in (
                ("Reifen", "tires"),
                ("Strecken", "streets"),
                ("Fahrweise", "driving_style"),
            ):
                record[key] = _number(row.get(column, ""), column, line)
            if record["odometer"] is None:
                raise CSVImportError(f"Zeile {line}: der Tachostand fehlt.")
            if tank_type in {1, 2} and (
                record["quantity"] is None
            ):
                raise CSVImportError(
                    f"Zeile {line}: die Tankmenge fehlt."
                )
            if record["latitude"] is not None and abs(record["latitude"]) > 90:
                raise CSVImportError(f"Zeile {line}: ungültiger Breitengrad.")
            if record["longitude"] is not None and abs(record["longitude"]) > 180:
                raise CSVImportError(f"Zeile {line}: ungültiger Längengrad.")
            records.append(record)
    except csv.Error as err:
        raise CSVImportError(
            f"Ungültige CSV in Zeile {reader.line_num}: {err}"
        ) from err
    if not records:
        raise CSVImportError("Die CSV enthält keine Tankvorgänge.")
    return records


def normalize_fueling(record, vehicle_type):
    """Keep API IDs and extra fields while normalizing the shared identity fields."""
    result = dict(record)
    result["date"] = fueling_date(result["date"]).strftime("%d.%m.%Y")
    # A pure EV's API tank ID need not be 2. Use one canonical electric tank
    # inside the ledger so the same charge from the CSV can be reconciled.
    result["tankid"] = (
        2 if vehicle_type == "electric" else int(result.get("tankid") or 1)
    )
    for key in NUMERIC_COLUMNS.values():
        value = result.get(key)
        if value not in (None, ""):
            result[key] = float(str(value).replace(",", "."))
    if isinstance(result.get("type"), int):
        result["type"] = FUELING_TYPES.get(result["type"], "invalid")
    return result


def fueling_key(record):
    """CSV exports have no API ID: compare date, tank, odometer and quantity."""
    return (
        record["date"],
        int(record["tankid"]),
        round(float(record.get("odometer") or 0), 2),
        round(float(record.get("quantity") or 0), 2),
    )


def merge_fuelings(existing, incoming, vehicle_type):
    """Deduplicate within an export, across imports, and against cached API data."""
    records = {fueling_key(record): dict(record) for record in existing}
    ids = {
        str(record["id"]): key
        for key, record in records.items()
        if record.get("id") is not None
    }
    added = duplicates = 0
    for raw in incoming:
        record = normalize_fueling(raw, vehicle_type)
        key = fueling_key(record)
        api_id = str(record["id"]) if record.get("id") is not None else None
        previous_key = ids.get(api_id, key)
        old = records.pop(previous_key, None)
        if old is None:
            added += 1
            merged = record
        else:
            duplicates += 1
            merged = {**old, **{k: v for k, v in record.items() if v not in (None, "")}}
            if old.get("_csv"):
                merged["_csv"] = True
        records[key] = merged
        if api_id is not None:
            ids[api_id] = key
    return sorted(records.values(), key=fueling_sort_key), added, duplicates


def fueling_sort_key(record):
    """Odometers order separate refuelings on the same date."""
    return (
        fueling_date(record["date"]),
        float(record.get("odometer") or 0),
        int(record["tankid"]),
    )


def valid_fuelings(records):
    """Exclude invalid entries; zero-valued measurements remain valid."""
    return [record for record in records if record.get("type") not in ("invalid", 0)]


def is_inverse_consumption(unit):
    """Distance per quantity improves when the value increases."""
    return unit in {"km/L", "km/l", "mi/gal", "mpg", "km/kWh", "mi/kWh"}


def display_consumption(value, unit):
    """Convert a quantity-per-100-distance value to the configured display unit."""
    if value is None:
        return None
    if is_inverse_consumption(unit):
        return 100 / value if value > 0 else None
    return value


def data_with_fuelings(data, records, vehicle_type):
    """Preserve main's tank separation and stable, date-only API ordering."""
    def date_key(record):
        return datetime.strptime(record["date"], "%d.%m.%Y")

    if vehicle_type == "electric":
        gas = []
        electric = sorted(records, key=date_key, reverse=True)
    else:
        gas = sorted(
            [record for record in records if record.get("tankid") == 1],
            key=date_key,
            reverse=True,
        )
        electric = sorted(
            [record for record in records if record.get("tankid") == 2],
            key=date_key,
            reverse=True,
        )
    primary = electric if vehicle_type == "electric" else gas
    return {
        **data,
        "all_fuelings": list(records),
        "refuelings": primary,
        "last_refueling": primary[0] if primary else None,
        "gas_refuelings": gas,
        "last_gas_refueling": gas[0] if gas else None,
        "electric_charges": electric,
        "last_electric_charge": electric[0] if electric else None,
    }
