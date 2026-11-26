import argparse
import json
from dataclasses import asdict
from pathlib import Path

import psycopg
from pydantic import AwareDatetime, TypeAdapter

from .channel_export import export_channels
from .history_registry import HistoryError, Version, load_layer, load_registry
from .importer import Slug, import_exercise
from .live_chat import LiveError, register_room, work
from .live_export import export_snapshot
from . import channel_capture, source_catalog
from . import pilot_admin
from .source_adapters import Label
from .storage import migrate
from .transcription import PROFILES


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up and run local exercise review.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Apply database migrations.")
    importer = commands.add_parser("import", help="Import an exercise manifest and its source files.")
    importer.add_argument("manifest", type=Path)
    worker = commands.add_parser("transcribe", help="Create a new import bundle with local speech-to-text.")
    worker.add_argument("manifest", type=Path)
    worker.add_argument("output", type=Path)
    worker.add_argument("--model", default="small.en")
    worker.add_argument("--language", default="en")
    worker.add_argument("--exercise-id", help="Set a separate exercise ID for the automatic transcript.")
    worker.add_argument("--offline", action="store_true", help="Use only cached model weights.")
    worker.add_argument("--profile", choices=PROFILES, default="standard",
                        help="Use the radio profile for recorded push-to-talk audio.")
    worker.add_argument("--radio-fixes", type=Path,
                        help="Suggest chart fix spellings from a JSON vocabulary for the selected radio sources.")
    sandbox = commands.add_parser("register-live", help="Register one sandbox chat room for an imported exercise.")
    sandbox.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    sandbox.add_argument("room", type=TypeAdapter(Slug).validate_python)
    classifier = commands.add_parser("classify-live", help="Classify sandbox messages stored in PostgreSQL.")
    classifier.add_argument("--once", action="store_true", help="Process at most one batch, then exit.")
    radio = commands.add_parser("transcribe-live", help="Transcribe completed radio captures with a local worker.")
    radio.add_argument("--model", default="small.en")
    radio.add_argument("--once", action="store_true", help="Process at most one queued clip and exit.")
    radio.add_argument("--offline", action="store_true", help="Use only cached model weights.")
    snapshot = commands.add_parser("export-live", help="Freeze live chat for review and publication as a separate exercise.")
    snapshot.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    snapshot.add_argument("review_exercise", type=TypeAdapter(Slug).validate_python)
    snapshot.add_argument("output", type=Path)
    channels = commands.add_parser("export-channels", help="Freeze multi-channel originals for review and publication.")
    channels.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    channels.add_argument("review_exercise", type=TypeAdapter(Slug).validate_python)
    channels.add_argument("output", type=Path)
    channels.add_argument("--known-at", required=True, type=TypeAdapter(AwareDatetime).validate_python)
    catalog = commands.add_parser("sources", help="Register, ingest, and replay catalog sources.")
    source_commands = catalog.add_subparsers(dest="source_command", required=True)
    registration = source_commands.add_parser("register", help="Register sources from a JSON catalog.")
    registration.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    registration.add_argument("config", type=Path)
    for name, help_text in (
        ("ingest", "Capture a local CSV or JSON file."),
        ("fetch-weather", "Fetch recent observations for the registered NWS station."),
        ("replay", "Derive a new parser version from an archived capture."),
    ):
        action = source_commands.add_parser(name, help=help_text)
        action.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
        action.add_argument("source", type=TypeAdapter(Slug).validate_python)
        if name == "ingest":
            action.add_argument("file", type=Path)
            action.add_argument("--parser-version", type=int)
        if name == "replay":
            action.add_argument("capture_id", type=int)
            action.add_argument("--parser-version", type=int, required=True)
    history = commands.add_parser("history", help="Publish reviewed entity aliases and offline map layers.")
    history_commands = history.add_subparsers(dest="history_command", required=True)
    registry = history_commands.add_parser("registry", help="Publish a reviewed registry JSON revision.")
    registry.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    registry.add_argument("file", type=Path)
    layer = history_commands.add_parser("layer", help="Publish a versioned local GeoJSON layer.")
    layer.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    layer.add_argument("key", type=TypeAdapter(Slug).validate_python)
    layer.add_argument("version", type=TypeAdapter(Version).validate_python)
    layer.add_argument("effective_at", type=TypeAdapter(AwareDatetime).validate_python)
    layer.add_argument("file", type=Path)
    layer.add_argument("--label", type=TypeAdapter(Label).validate_python, required=True)
    pilot_admin.add_commands(commands)
    args = parser.parse_args()

    try:
        if args.command == "migrate":
            migrate()
            print("Database migrations are up to date.")
        elif args.command == "import":
            print(json.dumps(asdict(import_exercise(args.manifest))))
        elif args.command == "register-live":
            register_room(args.exercise, args.room)
            print(f"Registered sandbox room {args.room} for {args.exercise}.")
        elif args.command == "classify-live":
            if args.once:
                print(json.dumps({"classified": work(once=True)}))
            else:
                work()
        elif args.command == "transcribe-live":
            print(json.dumps({"processed": channel_capture.work_radio(
                args.model, once=args.once, offline=args.offline
            )}))
        elif args.command == "export-live":
            count = export_snapshot(args.exercise, args.review_exercise, args.output)
            print(f"Exported {count} messages to {args.output}.")
        elif args.command == "export-channels":
            print(json.dumps(export_channels(args.exercise, args.review_exercise, args.output, known_at=args.known_at)))
        elif args.command == "sources":
            run_source_command(args)
        elif args.command == "history":
            receipt = (load_registry(args.exercise, args.file) if args.history_command == "registry"
                       else load_layer(args.exercise, args.key, args.version, args.label, args.effective_at, args.file))
            print(json.dumps(receipt, default=lambda value: value.isoformat()))
        elif args.command == "pilot":
            result = pilot_admin.run(args)
            print(result if isinstance(result, str) else json.dumps(result, default=str, indent=2))
        else:
            from .transcription import build_bundle

            report = build_bundle(args.manifest, args.output, model_name=args.model, language=args.language,
                                  exercise_id=args.exercise_id, offline=args.offline, profile=args.profile,
                                  radio_fixes=args.radio_fixes)
            print(json.dumps(report, indent=2))
    except (LiveError, source_catalog.CatalogError, HistoryError) as exc:
        parser.exit(1, f"{args.command} failed: {exc.detail}\n")
    except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
        parser.exit(1, f"{args.command} failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130)


def run_source_command(args) -> None:
    if args.source_command == "register":
        print(json.dumps({"registered": source_catalog.register(args.exercise, args.config)}))
        return
    if args.source_command == "ingest":
        receipt = source_catalog.ingest_file(args.exercise, args.source, args.file, args.parser_version)
    elif args.source_command == "fetch-weather":
        receipt = source_catalog.fetch_weather(args.exercise, args.source)
    else:
        receipt = source_catalog.replay(args.exercise, args.source, args.capture_id, args.parser_version)
    print(json.dumps(asdict(receipt)))
    if receipt.state in {"quarantined", "unavailable"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
