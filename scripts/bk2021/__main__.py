"""Run and inspect the BK2021 feed using the SignalForce database."""

import argparse
import json
import sys
from pathlib import Path

import yaml

from scripts.bk2021.export import export_csv
from scripts.bk2021.models import TenantConfig
from scripts.bk2021.runner import reassign, status, tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.bk2021")
    parser.add_argument("--config", required=True, type=Path, help="Tenant and feed YAML")
    commands = parser.add_subparsers(dest="command", required=True)
    tick_command = commands.add_parser("tick", help="Run feeds whose interval has elapsed")
    tick_command.add_argument("--feed", help="Select one configured feed key")
    tick_command.add_argument("--force", action="store_true", help="Ignore the selected intervals")
    commands.add_parser("status", help="Show feed state and unassigned announcements")
    commands.add_parser(
        "reassign", help="Apply the configured verified NIP mappings to stored signals"
    )
    commands.add_parser("export", help="Write one active tender per verified domain to CSV")
    args = parser.parse_args(argv)
    try:
        config = TenantConfig.load(args.config)
        match args.command:
            case "tick":
                results = tick(config, feed_key=args.feed, force=args.force)
                print(json.dumps([row.model_dump() for row in results], indent=2))
                return int(any(row.state == "failed" for row in results))
            case "status":
                print(status(config).model_dump_json(indent=2))
            case "reassign":
                print(json.dumps({"reassigned": reassign(config)}))
            case "export":
                sys.stdout.write(export_csv(config))
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print(f"BK2021: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
