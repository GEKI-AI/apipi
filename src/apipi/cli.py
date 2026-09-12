import argparse
import sys

from apipi.config import ConfigError
from apipi.store.migrate import migrate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apipi")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply store migrations")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        try:
            migrate()
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    return 1
