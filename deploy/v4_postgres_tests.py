"""Inject this stack's test DSN privately into a project-local pytest process."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import quote

from v4_config import read_private, validate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("pytest_arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = read_private(args.env_file)
    validate(values)
    env = dict(os.environ)
    password = quote(values["QT_V4_TEST_POSTGRES_PASSWORD"], safe="")
    port = values["QT_V4_TEST_DB_PORT"]
    env["QT_V4_TEST_DATABASE_URL"] = f"postgresql+psycopg://qt_v4_test:{password}@127.0.0.1:{port}/qt_v4_test"
    arguments = args.pytest_arguments
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        arguments = ["-q", "tests/test_production_postgres.py"]
    os.execvpe(sys.executable, [sys.executable, "-m", "pytest", *arguments], env)


if __name__ == "__main__":
    main()
