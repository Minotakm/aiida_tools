#!/usr/bin/env python
"""Entry point for the AiiDA Error Inspector TUI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aiida-error-inspector",
        description=(
            "Browse AiiDA groups and workchains, and batch-tag failed "
            "calculations by error pattern or exit code."
        ),
    )
    parser.add_argument(
        "group",
        nargs="?",
        help="Group label, PK or UUID to open directly. Omit to browse all groups.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Where tags, classifiers and the scan cache live. Defaults to "
            f"${storage.ENV_DATA_DIR}, the repository's data/ directory when "
            "running from a checkout, or the platform data directory."
        ),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AiiDA profile to load (default: the configured default profile).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging to <data-dir>/aiida-error-inspector.log.",
    )
    return parser


def configure_logging(data_dir: Path, debug: bool) -> Path:
    """Log to our own logger, not the root one.

    ``logging.basicConfig(level=DEBUG)`` previously configured the *root*
    logger, which also switched on AiiDA and SQLAlchemy debug output and wrote
    the result inside the installed package directory.
    """
    log_path = data_dir / "aiida-error-inspector.log"
    logger = logging.getLogger("aiida_error_inspector")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(log_path)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return log_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    data_dir = storage.resolve_data_dir(args.data_dir)
    log_path = configure_logging(data_dir, args.debug)
    logging.getLogger("aiida_error_inspector").info(
        "Starting; data dir %s", data_dir
    )

    from aiida.manage.configuration import load_profile

    try:
        profile = load_profile(args.profile)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not load an AiiDA profile: {exc}", file=sys.stderr)
        return 2

    from .app import GroupNodesApp

    app = GroupNodesApp(group_identifier=args.group, data_dir=data_dir)
    app.run()

    print(f"Profile: {profile.name}   data: {data_dir}   log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
