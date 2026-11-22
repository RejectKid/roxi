import argparse
import json
from dataclasses import asdict
from pathlib import Path

import psycopg

from .importer import import_exercise
from .storage import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up and import completed exercise recordings.")
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
    args = parser.parse_args()

    try:
        if args.command == "migrate":
            migrate()
            print("Database migrations are up to date.")
        elif args.command == "import":
            print(json.dumps(asdict(import_exercise(args.manifest))))
        else:
            from .transcription import build_bundle

            report = build_bundle(args.manifest, args.output, model_name=args.model, language=args.language,
                                  exercise_id=args.exercise_id, offline=args.offline)
            print(json.dumps(report, indent=2))
    except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
        parser.exit(1, f"{args.command} failed: {exc}\n")


if __name__ == "__main__":
    main()
