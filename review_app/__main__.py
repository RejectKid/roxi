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
    args = parser.parse_args()

    try:
        if args.command == "migrate":
            migrate()
            print("Database migrations are up to date.")
        else:
            print(json.dumps(asdict(import_exercise(args.manifest))))
    except (OSError, ValueError, psycopg.Error) as exc:
        parser.exit(1, f"{args.command} failed: {exc}\n")


if __name__ == "__main__":
    main()
