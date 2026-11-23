"""Versioned inputs and evidence locators for the local civilian source catalog."""

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from .importer import Slug

CONTRACT_VERSION = 1
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
STATION = re.compile(r"^[A-Z0-9]{3,8}$")
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160, pattern=r"^[^\x00]*$")]
Latitude = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]
NonnegativeSpeed = Annotated[float, Field(ge=0, allow_inf_nan=False)]
GeographicPoint = TypeAdapter(tuple[Longitude, Latitude], config=ConfigDict(strict=True))


@dataclass(frozen=True)
class Locator:
    kind: Literal["csv_row", "json_pointer"]
    value: int | str


@dataclass(frozen=True)
class Record:
    record_id: str
    subject_id: str
    kind: str
    observed_at: datetime
    data: dict
    locator: Locator


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str
    locator: Locator


@dataclass(frozen=True)
class Parsed:
    records: list[Record]
    issues: list[Issue]

    @property
    def state(self) -> str:
        if not self.records:
            return "quarantined"
        return "partial" if self.issues else "accepted"


@dataclass(frozen=True)
class Adapter:
    key: str
    input_schema: str
    versions: tuple[int, ...]
    parse_payload: Callable[[bytes, int], Parsed]

    def parse(self, payload: bytes, version: int | None = None) -> Parsed:
        return self.parse_payload(payload, self.select_version(version))

    def select_version(self, version: int | None) -> int:
        selected = self.versions[-1] if version is None else version
        if selected not in self.versions:
            raise ValueError(f"{self.key} supports parser versions {', '.join(map(str, self.versions))}.")
        return selected


class AssetRow(BaseModel):
    asset_id: Slug
    name: Label
    asset_type: Label
    site_id: Slug
    updated_at: AwareDatetime


class SiteRow(BaseModel):
    site_id: Slug
    name: Label
    latitude: Latitude
    longitude: Longitude
    updated_at: AwareDatetime


class FleetItem(BaseModel):
    model_config = ConfigDict(strict=True)

    vehicle_id: Slug
    observed_at: AwareDatetime
    latitude: Latitude
    longitude: Longitude
    status: Literal["parked", "en_route", "loading", "offline"]
    speed_mph: NonnegativeSpeed | None = None


class Reading(BaseModel):
    model_config = ConfigDict(strict=True)

    value: Annotated[float, Field(allow_inf_nan=False)] | None
    unitCode: Label


class WeatherItem(BaseModel):
    model_config = ConfigDict(strict=True)

    station: Label
    timestamp: AwareDatetime
    temperature: Reading | None = None
    windSpeed: Reading | None = None


def invalid_field(error: ValidationError) -> str:
    fields = sorted({str(item["loc"][0]) for item in error.errors(include_url=False, include_input=False) if item["loc"]})
    return f"Invalid or missing fields: {', '.join(fields)}." if fields else "Expected an object with the required fields."


def finish(records: list[Record], issues: list[Issue], root: Locator) -> Parsed:
    if not records and not issues:
        issues.append(Issue("empty_capture", "The capture contains no records.", root))
    return Parsed(records, issues)


def csv_rows(payload: bytes, model: type[AssetRow] | type[SiteRow], id_field: str, kind: str) -> Parsed:
    root = Locator("csv_row", 1)
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return Parsed([], [Issue("invalid_encoding", "Expected UTF-8 CSV.", root)])

    records, issues, seen = [], [], set()
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    locator = root
    try:
        header = next(reader, [])
        required = model.model_fields.keys()
        if not required <= set(header) or len(set(header)) != len(header):
            return Parsed([], [Issue("invalid_header", f"Expected columns {', '.join(required)}.", root)])
        while True:
            locator = Locator("csv_row", reader.line_num + 1)
            row = next(reader, None)
            if row is None:
                break
            if not row:
                continue
            if len(row) != len(header):
                issues.append(Issue("invalid_row", "Field count does not match the CSV header.", locator))
                continue
            try:
                item = model.model_validate(dict(zip(header, row, strict=True)))
            except ValidationError as error:
                issues.append(Issue("invalid_row", invalid_field(error), locator))
                continue
            key = getattr(item, id_field)
            if key in seen:
                issues.append(Issue("duplicate_record", "The identifier occurs more than once in this CSV.", locator))
                continue
            seen.add(key)
            records.append(Record(key, key, kind, item.updated_at, item.model_dump(mode="json"), locator))
    except csv.Error:
        issues.append(Issue("invalid_csv", "Malformed CSV quoting or field length.", locator))
    return finish(records, issues, root)


def read_json(payload: bytes) -> dict | None:
    try:
        document = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    return document if isinstance(document, dict) else None


def json_records(
    rows: list, pointer: str, normalize: Callable[[Any, Locator], tuple[Record, list[Issue]]],
) -> Parsed:
    records, issues, seen = [], [], set()
    for index, item in enumerate(rows):
        locator = Locator("json_pointer", f"{pointer}/{index}")
        try:
            record, gaps = normalize(item, locator)
        except ValidationError as error:
            issues.append(Issue("invalid_record", invalid_field(error), locator))
            continue
        except (ValueError, RecursionError) as error:
            issues.append(Issue("invalid_record", str(error), locator))
            continue
        if record.record_id in seen:
            issues.append(Issue("duplicate_record", "The same observation occurs more than once.", locator))
            continue
        seen.add(record.record_id)
        records.append(record)
        issues.extend(gaps)
    return finish(records, issues, Locator("json_pointer", pointer))


def fleet(payload: bytes, version: int) -> Parsed:
    document = read_json(payload)
    if not document or document.get("schema") != "roxi.civilian-fleet/v1" or not isinstance(document.get("vehicles"), list):
        return Parsed([], [Issue("invalid_envelope", "Expected roxi.civilian-fleet/v1 with a vehicles array.",
                                   Locator("json_pointer", ""))])

    def normalize(value, locator):
        # JSON mode keeps timestamp validation strict without coercing strings into coordinates.
        item = FleetItem.model_validate_json(json.dumps(value))
        data = item.model_dump(mode="json", exclude={"observed_at"})
        if version == 2:
            data["status"] = "in_transit" if item.status == "en_route" else item.status
            data["speed_m_s"] = round(item.speed_mph * 0.44704, 4) if item.speed_mph is not None else None
        key = f"{item.vehicle_id}@{item.observed_at.timestamp()}"
        return Record(key, item.vehicle_id, "vehicle_observation", item.observed_at, data, locator), []

    return json_records(document["vehicles"], "/vehicles", normalize)


def weather(payload: bytes, version: int) -> Parsed:
    document = read_json(payload)
    if not document or document.get("type") != "FeatureCollection" or not isinstance(document.get("features"), list):
        return Parsed([], [Issue("invalid_envelope", "Expected an NWS observation FeatureCollection.",
                                   Locator("json_pointer", ""))])

    def normalize(value, locator):
        if not isinstance(value, dict) or value.get("type") != "Feature" or not isinstance(value.get("properties"), dict):
            raise ValueError("Expected an NWS Feature with observation properties.")
        item = WeatherItem.model_validate_json(json.dumps(value["properties"]))
        station = item.station.rstrip("/").rsplit("/", 1)[-1]
        if not STATION.fullmatch(station):
            raise ValueError("Expected a station identifier at the end of the station reference.")
        data = {"station_id": station}
        gaps = []
        for field, reading in (("temperature", item.temperature), ("windSpeed", item.windSpeed)):
            data[field] = reading.model_dump() if reading else None
            if reading is None or reading.value is None:
                gaps.append(Issue("missing_measurement", f"{field} was unavailable.",
                                  Locator("json_pointer", f"{locator.value}/properties/{field}")))
        if version == 2 and value.get("geometry") is not None:
            shape = value["geometry"]
            coordinates = shape.get("coordinates") if isinstance(shape, dict) and shape.get("type") == "Point" else None
            try:
                if not isinstance(coordinates, list) or len(coordinates) not in {2, 3}:
                    raise ValueError("Expected a point with longitude and latitude.")
                longitude, latitude = GeographicPoint.validate_python(tuple(coordinates[:2]))
                data.update(longitude=longitude, latitude=latitude)
            except ValueError:
                gaps.append(Issue("invalid_position", "The station position was invalid.",
                                  Locator("json_pointer", f"{locator.value}/geometry")))
        key = f"{station}@{item.timestamp.timestamp()}"
        return Record(key, station, "weather_observation", item.timestamp, data, locator), gaps

    return json_records(document["features"], "/features", normalize)


ADAPTERS = {
    item.key: item for item in (
        Adapter("assets-csv", "roxi.assets-csv/v1", (1,), lambda data, _: csv_rows(data, AssetRow, "asset_id", "asset")),
        Adapter("sites-csv", "roxi.sites-csv/v1", (1,), lambda data, _: csv_rows(data, SiteRow, "site_id", "site")),
        Adapter("civilian-fleet", "roxi.civilian-fleet/v1", (1, 2), fleet),
        Adapter("nws-observations", "nws.observation-geojson/v1", (1, 2), weather),
    )
}
