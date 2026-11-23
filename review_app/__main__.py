import argparse
import json
from dataclasses import asdict
from pathlib import Path

import psycopg
from pydantic import TypeAdapter

from .importer import Slug, import_exercise
from .live_chat import LiveError, register_room, work
from .live_export import export_snapshot
from .storage import migrate


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
    sandbox = commands.add_parser("register-live", help="Register one sandbox chat room for an imported exercise.")
    sandbox.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    sandbox.add_argument("room", type=TypeAdapter(Slug).validate_python)
    classifier = commands.add_parser("classify-live", help="Classify sandbox messages stored in PostgreSQL.")
    classifier.add_argument("--once", action="store_true", help="Process at most one batch, then exit.")
    snapshot = commands.add_parser("export-live", help="Freeze live chat for review and publication as a separate exercise.")
    snapshot.add_argument("exercise", type=TypeAdapter(Slug).validate_python)
    snapshot.add_argument("review_exercise", type=TypeAdapter(Slug).validate_python)
    snapshot.add_argument("output", type=Path)
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
        elif args.command == "export-live":
            count = export_snapshot(args.exercise, args.review_exercise, args.output)
            print(f"Exported {count} messages to {args.output}.")
        else:
            from .transcription import build_bundle

            report = build_bundle(args.manifest, args.output, model_name=args.model, language=args.language,
                                  exercise_id=args.exercise_id, offline=args.offline)
            print(json.dumps(report, indent=2))
    except LiveError as exc:
        parser.exit(1, f"{args.command} failed: {exc.detail}\n")
    except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
        parser.exit(1, f"{args.command} failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130)


if __name__ == "__main__":
    main()
