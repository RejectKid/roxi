"""Load the fictional West Annex replay and report its real receipt-time cutoffs."""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "history"
EXERCISE = "west-annex-history"
ROOM = "receiving-dock"
sys.path.insert(0, str(ROOT))


def json_file(path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_chat() -> list[datetime]:
    from review_app.live_chat import ChatMessage, receive, register_room, work
    from review_app.storage import connect_read

    register_room(EXERCISE, ROOM)
    ids = []
    for item in json_file(FIXTURE / "chat.json"):
        message = ChatMessage.model_validate(item)
        original = json.dumps(item, separators=(",", ":")).encode()
        ids.append(receive(EXERCISE, ROOM, message, original)["id"])
    deadline = monotonic() + 10
    while monotonic() < deadline:
        with connect_read() as conn:
            rows = conn.execute(
                "SELECT classified_at FROM live_chat_version WHERE id = ANY(%s) AND classified_at IS NOT NULL",
                (ids,),
            ).fetchall()
        if len(rows) == len(ids):
            return [row["classified_at"] for row in rows]
        work(once=True)
        sleep(0.05)
    raise RuntimeError("The fictional chat replay did not finish classifying.")


def derived_at(capture_id: int, version: int) -> datetime:
    from review_app.storage import connect_read

    with connect_read() as conn:
        return conn.execute(
            "SELECT derived_at FROM catalog_derivation WHERE capture_id = %s AND parser_version = %s",
            (capture_id, version),
        ).fetchone()["derived_at"]


def view_url(as_of, known_at) -> str:
    return f"/exercises/{EXERCISE}/history?" + urlencode({"as_of": as_of, "known_at": known_at.isoformat()})


def seed() -> dict:
    from review_app import source_catalog as catalog
    from review_app.history_registry import load_layer, load_registry
    from review_app.importer import import_exercise

    import_exercise(FIXTURE / "input" / "exercise.json")
    catalog.register(EXERCISE, FIXTURE / "catalog.json")
    registry = load_registry(EXERCISE, FIXTURE / "registry-v1.json")
    layer = load_layer(EXERCISE, "receiving", 1, "Receiving layout",
                       datetime.fromisoformat("2026-11-22T14:55:00Z"), FIXTURE / "layout-v1.geojson")
    known_times = [registry["reviewed_at"], layer["recorded_at"]]
    for key, filename, parser in (
        ("assets", "assets.csv", 1), ("sites", "sites.csv", 1),
        ("fleet", "fleet-on-time.json", 1), ("weather", "weather.geojson", 2),
    ):
        result = catalog.ingest_file(EXERCISE, key, FIXTURE / filename, parser)
        if result.state != "accepted":
            raise RuntimeError(f"The fictional {key} capture was not accepted.")
        known_times.append(derived_at(result.capture_id, result.parser_version))
    known_times.extend(load_chat())
    before = max(known_times)

    late = catalog.ingest_file(EXERCISE, "fleet", FIXTURE / "fleet-late.json", 1)
    if late.state != "accepted":
        raise RuntimeError("The fictional late fleet capture was not accepted.")
    after = derived_at(late.capture_id, 1)

    revised_registry = load_registry(EXERCISE, FIXTURE / "registry-v2.json")
    revised_layer = load_layer(EXERCISE, "receiving", 2, "Receiving layout",
                               datetime.fromisoformat("2026-11-22T15:02:00Z"), FIXTURE / "layout-v2.geojson")
    catalog.replay(EXERCISE, "fleet", late.capture_id, 2)
    revised = max(revised_registry["reviewed_at"], revised_layer["recorded_at"], derived_at(late.capture_id, 2))
    if not before < after < revised:
        raise RuntimeError("The fictional exercise already contains out-of-order receipt-time phases.")

    as_of = json_file(FIXTURE / "answer-key.json")["as_of"]
    return {"exercise_id": EXERCISE, "as_of": as_of, "late_capture_id": late.capture_id,
            "known_at": {key: stamp.isoformat() for key, stamp in
                         (("before_late", before), ("after_late", after), ("after_revisions", revised))},
            "views": {key: view_url(as_of, stamp) for key, stamp in
                      (("before_late", before), ("after_late", after), ("after_revisions", revised))}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save reproducible cutoffs as JSON.")
    args = parser.parse_args()
    report = json.dumps(seed(), indent=2) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
