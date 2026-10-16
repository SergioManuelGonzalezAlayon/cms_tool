"""Configurable child process used by hardware-free subprocess tests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time


def _emit_repeated(stream, prefix: str, count: int) -> None:
    for index in range(count):
        stream.write(f"{prefix}-{index}\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        action="append",
        default=[],
        help="Action in kind:value form; actions execute in the supplied order.",
    )
    parser.add_argument("--cleanup-marker", default="")
    args = parser.parse_args(argv)

    try:
        for action in args.action:
            kind, separator, value = action.partition(":")
            if not separator:
                raise ValueError(f"invalid fake child action: {action}")
            if kind == "stdout":
                print(value, file=sys.stdout, flush=True)
            elif kind == "stderr":
                print(value, file=sys.stderr, flush=True)
            elif kind == "pause":
                time.sleep(float(value))
            elif kind == "stdout-repeat":
                _emit_repeated(sys.stdout, "stdout", int(value))
            elif kind == "stderr-repeat":
                _emit_repeated(sys.stderr, "stderr", int(value))
            elif kind == "exit":
                return int(value)
            elif kind == "abrupt-exit":
                os._exit(int(value))
            else:
                raise ValueError(f"unknown fake child action: {kind}")
    finally:
        if args.cleanup_marker:
            Path(args.cleanup_marker).write_text("cleanup\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
