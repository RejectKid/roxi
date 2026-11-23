import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .importer import Manifest
from .live_chat import LiveError, current_items, room_for
from .storage import connect_read
from .transcription import timestamp


def export_snapshot(exercise_id: str, review_id: str, output: Path) -> int:
    if review_id == exercise_id:
        raise ValueError("Use a separate exercise ID for the frozen review snapshot.")
    if output.exists():
        raise ValueError(f"Output already exists: {output}")

    with connect_read() as conn:
        room_for(conn, exercise_id)
        exercise = conn.execute(
            "SELECT name, glossary, started_at_utc FROM exercise WHERE id = %s", (exercise_id,),
        ).fetchone()
        records = list(reversed(current_items(conn, exercise_id, limit=None)))
    if not records:
        raise LiveError(409, "Classify at least one sandbox message before exporting.")

    manifest = Manifest.model_validate({
        "exercise_id": review_id,
        "name": f"{exercise['name'][:141]} (frozen review)",
        "started_at_utc": exercise["started_at_utc"],
        "glossary_file": "glossary.json",
        "sources": [{"source_key": "live-snapshot", "kind": "chat", "file": "live-snapshot.irc",
                     "time_basis": "exercise_elapsed_ms"}],
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".live-export-", dir=output.parent) as directory:
        bundle = Path(directory) / "bundle"
        bundle.mkdir()
        lines, sources = [], []
        for number, record in enumerate(records, 1):
            sent = timestamp(record["sent_ms"])
            lines.append(f"[{sent}] <{record['sender']}> {record['body']}\n")
            sources.append({
                "line": number, "message_id": record["message_id"], "version": record["version"],
                "original_path": f"/exercises/{exercise_id}/live/evidence/{record['id']}/original",
            })
        (bundle / "live-snapshot.irc").write_text("".join(lines), encoding="utf-8", newline="\n")
        (bundle / "exercise.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        for name, value in (("glossary.json", exercise["glossary"]), ("source-map.json", sources)):
            (bundle / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        bundle.rename(output)
    return len(records)
