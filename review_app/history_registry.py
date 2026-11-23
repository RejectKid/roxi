from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .importer import Slug
from .live_chat import connect_editor
from .source_adapters import Label, Latitude, Longitude
from .source_catalog import read_bounded
from .storage import archive_capture, archive_writer, connect_read

Version = Annotated[int, Field(gt=0, le=2**31 - 1)]
Namespace = Annotated[str, StringConstraints(pattern=r"^(catalog|chat):[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=104)]
Position = tuple[Longitude, Latitude]


@dataclass
class HistoryError(Exception):
    status: int
    detail: str


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entity(RegistryModel):
    id: Slug
    label: Label
    kind: Literal["asset", "site"]


class Alias(RegistryModel):
    source: Namespace
    external_id: Label
    entities: Annotated[list[Slug], Field(min_length=1, max_length=25)]


class Registry(RegistryModel):
    version: Version
    reviewer: Label
    entities: Annotated[list[Entity], Field(min_length=1, max_length=500)]
    aliases: Annotated[list[Alias], Field(max_length=2000)]

    @model_validator(mode="after")
    def validate_membership(self):
        ids = {entry.id for entry in self.entities}
        if len(ids) != len(self.entities):
            raise ValueError("Canonical entity IDs must be unique.")
        keys = {(alias.source, alias.external_id.casefold()) for alias in self.aliases}
        if len(keys) != len(self.aliases):
            raise ValueError("List all candidates for an alias in one entry.")
        if any(not set(alias.entities) <= ids or len(set(alias.entities)) != len(alias.entities) for alias in self.aliases):
            raise ValueError("Alias candidates must be distinct canonical entity IDs from this registry.")
        return self


class Point(RegistryModel):
    type: Literal["Point"]
    coordinates: Position


class Line(RegistryModel):
    type: Literal["LineString"]
    coordinates: Annotated[list[Position], Field(min_length=2, max_length=2000)]


Ring = Annotated[list[Position], Field(min_length=4, max_length=2000)]


class Polygon(RegistryModel):
    type: Literal["Polygon"]
    coordinates: Annotated[list[Ring], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def closed_rings(self):
        if any(ring[0] != ring[-1] for ring in self.coordinates):
            raise ValueError("Polygon rings must be closed.")
        return self


class FeatureProperties(RegistryModel):
    label: Label


class Feature(RegistryModel):
    type: Literal["Feature"]
    id: Slug
    geometry: Annotated[Point | Line | Polygon, Field(discriminator="type")]
    properties: FeatureProperties


class Layer(RegistryModel):
    type: Literal["FeatureCollection"]
    features: Annotated[list[Feature], Field(min_length=1, max_length=250)]

    @model_validator(mode="after")
    def feature_ids(self):
        if len({item.id for item in self.features}) != len(self.features):
            raise ValueError("Layer feature IDs must be unique.")
        return self


def manifest_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        return read_bounded(stream)


def exercise_for(conn, exercise_id: str) -> dict:
    row = conn.execute("SELECT id, name FROM exercise WHERE id = %s", (exercise_id,)).fetchone()
    if row is None:
        raise HistoryError(404, "Exercise not found.")
    return row


def lock_versions(conn, exercise_id: str) -> None:
    exercise_for(conn, exercise_id)
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"history:{exercise_id}",))


@archive_writer
def load_registry(exercise_id: str, path: Path) -> dict:
    payload = manifest_bytes(path)
    registry = Registry.model_validate_json(payload)
    original = archive_capture(payload)
    with connect_editor() as conn:
        lock_versions(conn, exercise_id)
        current = conn.execute(
            "SELECT version, digest, reviewed_at FROM history_registry "
            "WHERE exercise_id = %s ORDER BY version DESC LIMIT 1", (exercise_id,),
        ).fetchone()
        if current and current["version"] >= registry.version:
            found = conn.execute(
                "SELECT version, digest, reviewed_at FROM history_registry WHERE exercise_id = %s AND version = %s",
                (exercise_id, registry.version),
            ).fetchone()
            if found and found["digest"] == original.sha256:
                return found
            raise HistoryError(409, "Publish reviewed aliases with a new, higher registry version.")
        return conn.execute(
            """
            INSERT INTO history_registry (exercise_id, version, digest, registry)
            VALUES (%s, %s, %s, %s) RETURNING version, digest, reviewed_at
            """,
            (exercise_id, registry.version, original.sha256, Jsonb(registry.model_dump(mode="json"))),
        ).fetchone()


@archive_writer
def load_layer(exercise_id: str, layer_key: str, version: int, label: str, effective: datetime, path: Path) -> dict:
    payload = manifest_bytes(path)
    layer = Layer.model_validate_json(payload)
    original = archive_capture(payload)
    with connect_editor() as conn:
        lock_versions(conn, exercise_id)
        current = conn.execute(
            """
            SELECT version, digest, label, effective_at, recorded_at FROM history_layer
            WHERE exercise_id = %s AND layer_key = %s ORDER BY version DESC LIMIT 1
            """,
            (exercise_id, layer_key),
        ).fetchone()
        if current and current["version"] >= version:
            found = conn.execute(
                """
                SELECT version, digest, label, effective_at, recorded_at FROM history_layer
                WHERE exercise_id = %s AND layer_key = %s AND version = %s
                """,
                (exercise_id, layer_key, version),
            ).fetchone()
            if found and (found["digest"], found["label"], found["effective_at"]) == (original.sha256, label, effective):
                return found
            raise HistoryError(409, "Publish revised geography with a new, higher layer version.")
        return conn.execute(
            """
            INSERT INTO history_layer (exercise_id, layer_key, version, label, effective_at, digest, features)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING version, digest, label, effective_at, recorded_at
            """,
            (exercise_id, layer_key, version, label, effective, original.sha256,
             Jsonb(layer.model_dump(mode="json")["features"])),
        ).fetchone()


def original_digest(exercise_id: str, version: int, layer_key: str | None = None) -> str:
    with connect_read() as conn:
        if layer_key is None:
            row = conn.execute("SELECT digest FROM history_registry WHERE exercise_id = %s AND version = %s",
                               (exercise_id, version)).fetchone()
        else:
            row = conn.execute(
                "SELECT digest FROM history_layer WHERE exercise_id = %s AND layer_key = %s AND version = %s",
                (exercise_id, layer_key, version),
            ).fetchone()
    if not row:
        raise HistoryError(404, "History evidence not found.")
    return row["digest"]
