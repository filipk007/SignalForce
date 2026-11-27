"""Run and inspect the TED and RWDZ pilot using the SignalForce database."""

import argparse
import json
import sys
from pathlib import Path

import yaml

from scripts.intake.export import export_csv
from scripts.intake.models import IntakeConfig
from scripts.intake.runner import reassign, status, tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.intake")
    parser.add_argument("--config", required=True, type=Path, help="Tenant and source YAML")
    commands = parser.add_subparsers(dest="command", required=True)
    tick_command = commands.add_parser("tick", help="Import sources whose interval has elapsed")
    tick_command.add_argument("--source", choices=("ted", "rwdz"), help="Select one source")
    tick_command.add_argument("--force", action="store_true", help="Ignore the intervals")
    commands.add_parser("status", help="Show source health and the review queue")
    commands.add_parser("reassign", help="Apply the current operator checks to stored entries")
    commands.add_parser("export", help="Write one active signal per verified domain to CSV")
    args = parser.parse_args(argv)
    try:
        config = IntakeConfig.load(args.config)
        match args.command:
            case "tick":
                results = tick(config, source=args.source, force=args.force)
                print(json.dumps([row.model_dump() for row in results], indent=2))
                return int(any(row.state == "failed" for row in results))
            case "status":
                print(status(config).model_dump_json(indent=2))
            case "reassign":
                print(json.dumps({"reassigned": reassign(config)}))
            case "export":
                sys.stdout.write(export_csv(config))
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print(f"intake: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
