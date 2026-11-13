"""Command line of a step: ``run --in /in --out /out --params /in/params.json``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .manifest import DEFAULT_MANIFEST_PATH
from .runner import RunFunction, run_step


def build_parser(
    description: str = "Contract step (contract 1.0).",
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run", description=description)
    parser.add_argument(
        "--in", dest="in_dir", default="/in", help="input directory (default /in)"
    )
    parser.add_argument(
        "--out", dest="out_dir", default="/out", help="output directory (default /out)"
    )
    parser.add_argument(
        "--params", default=None, help="parameter file (default <in>/params.json)"
    )
    # Not part of the contract command; for running a step outside its image.
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST_PATH), help=argparse.SUPPRESS
    )
    parser.add_argument("--ref", default="/ref", help=argparse.SUPPRESS)
    return parser


def main(run_fn: RunFunction, argv: list[str] | None = None) -> int:
    """Entry point of ``/usr/local/bin/run``; returns the exit code."""
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    return run_step(
        run_fn,
        in_dir=Path(args.in_dir),
        out_dir=Path(args.out_dir),
        params_path=Path(args.params) if args.params else None,
        manifest_path=Path(args.manifest),
        ref_dir=Path(args.ref),
    )
