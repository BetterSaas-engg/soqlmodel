"""Command line interface. Thin: parse arguments, format output, pick an exit code.

Every command is a wrapper over :mod:`soqlmodel.project`. No schema logic lives
here, so the CLI can stay untested-by-necessity and the behaviour it exposes
stays tested where it is implemented.

argparse from the stdlib, deliberately: the project ships zero runtime
dependencies (D9) and this surface is three commands wide.
"""

import argparse
import dataclasses
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NoReturn

from soqlmodel.check import exit_code, format_report
from soqlmodel.config import CONFIG_FILENAME, Config, load_config
from soqlmodel.errors import ConfigError, SoqlModelError
from soqlmodel.project import (
    DEFAULT_SCHEMA_DIR,
    check_all,
    generate_all,
    snapshot_all,
)

DEFAULT_OUTPUT = "models.py"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

# Failures a user can cause and should read as one line, not a traceback.
#
# Narrow on purpose (D11). SoqlModelError is raised only where we decided a
# failure is the user's to fix; OSError covers the filesystem underneath.
# Nothing else belongs here — ValueError in particular, which the stdlib
# raises everywhere: catching it would dress a bug in the query builder up as
# a tidy "soqlmodel: ..." line and exit 2, turning a defect into a plausible
# answer. A traceback is the correct output for a bug.
_EXPECTED_ERRORS = (SoqlModelError, OSError)

_EPILOG = """\
global options:
  --config, --schema-dir and --org may be given before or after COMMAND.
  Given in both places, the one after COMMAND wins.

exit codes:
  0  success, or check found no CRITICAL drift
  1  check found CRITICAL drift
  2  usage error, bad config, or the org could not be reached
"""


def _package_version() -> str:
    try:
        return version("soqlmodel")
    except PackageNotFoundError:  # running from a source tree, not installed
        return "unknown"


def _common_options() -> argparse.ArgumentParser:
    """Options accepted either before or after the subcommand.

    SUPPRESS as the default is what makes both positions work: an option the
    user did not give leaves no attribute behind, so the subparser copy cannot
    overwrite a value given before the subcommand.

    That also decides the precedence, and the decision is deliberate: **the
    occurrence after COMMAND wins**. Argparse copies the subparser's namespace
    over the parent's, so an option given in both places resolves to the later
    one — the same rule argparse already applies to an option repeated in one
    position. One rule for both cases beats "it depends where you wrote it".
    Pinned by test, not left to argparse, and stated in ``--help``.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"path to the config file (default: {CONFIG_FILENAME})",
    )
    common.add_argument(
        "--schema-dir",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"directory holding the snapshots (default: {DEFAULT_SCHEMA_DIR}/)",
    )
    common.add_argument(
        "--org",
        metavar="NAME",
        default=argparse.SUPPRESS,
        help="org alias, overriding the one in the config",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="soqlmodel",
        description="Typed Python models and SOQL, generated from a Salesforce org.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument(
        "--version", action="version", version=f"soqlmodel {_package_version()}"
    )

    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")

    subcommands.add_parser(
        "snapshot",
        parents=[common],
        help="extract from the org and write schema/<SObject>.json",
        description=(
            "Extract every declared sObject and write its snapshot. Strict: a "
            "declared field the org does not have is an error."
        ),
    )

    generate = subcommands.add_parser(
        "generate",
        parents=[common],
        help="read schema/ and write the models module",
        description="Generate one module holding a class per committed snapshot.",
    )
    generate.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        default=DEFAULT_OUTPUT,
        help=f"where to write the module (default: {DEFAULT_OUTPUT})",
    )

    subcommands.add_parser(
        "check",
        parents=[common],
        help="diff committed snapshots against the org",
        description=(
            "Compare every committed snapshot against a fresh extract. "
            "Exits 1 if any CRITICAL drift is found."
        ),
    )

    return parser


def _load(args: argparse.Namespace) -> Config:
    """Load the config named by ``--config``, applying an ``--org`` override."""
    path = Path(getattr(args, "config", CONFIG_FILENAME))
    if not path.is_file():
        raise ConfigError(
            f"no config at {path}; create one declaring the sObjects this "
            "project depends on"
        )

    config = load_config(path)
    org = getattr(args, "org", None)
    return dataclasses.replace(config, org=org) if org else config


def _schema_dir(args: argparse.Namespace) -> str:
    return getattr(args, "schema_dir", DEFAULT_SCHEMA_DIR)


def _snapshot(args: argparse.Namespace) -> int:
    written = snapshot_all(_load(args), _schema_dir(args))
    for path in written:
        print(f"wrote {path}")
    print(f"{len(written)} snapshot(s) written.")
    return EXIT_OK


def _generate(args: argparse.Namespace) -> int:
    path = generate_all(_load(args), _schema_dir(args), args.output)
    print(f"wrote {path}")
    return EXIT_OK


def _check(args: argparse.Namespace) -> int:
    changes = check_all(_load(args), _schema_dir(args))
    print(format_report(changes))
    return exit_code(changes)


_COMMANDS = {"snapshot": _snapshot, "generate": _generate, "check": _check}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_ERROR

    try:
        return _COMMANDS[args.command](args)
    except _EXPECTED_ERRORS as exc:
        print(f"soqlmodel: {exc}", file=sys.stderr)
        return EXIT_ERROR


def run() -> NoReturn:
    """Console-script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
