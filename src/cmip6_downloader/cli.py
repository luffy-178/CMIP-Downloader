"""Command line interface: ``cmip6-download search|get|update-catalog``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .catalog import load_catalog, search
from .downloader import DEFAULT_DEST, CMIP6Downloader


def _add_facets(p: argparse.ArgumentParser, required: bool) -> None:
    p.add_argument("-v", "--variable", nargs="+", required=required, help="variable id(s), e.g. tos thetao")
    p.add_argument("-s", "--source", nargs="+", help="model(s), e.g. CESM2")
    p.add_argument("-e", "--experiment", nargs="+", help="experiment(s), e.g. historical ssp585")
    p.add_argument("-t", "--table", nargs="+", help="table(s), e.g. Omon Amon day")
    p.add_argument("-m", "--member", nargs="+", help="member(s), e.g. r1i1p1f1, or 'all'")
    p.add_argument("-g", "--grid", nargs="+", help="grid label(s), e.g. gn gr")
    p.add_argument("--mirror", choices=["gcs", "aws"], default="gcs")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmip6-download",
        description="Fast, subsetting downloader for CMIP6 data from the cloud mirrors.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-q", "--quiet", action="store_true", help="only print warnings")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="list the datasets matching the facets")
    _add_facets(p, required=False)
    p.add_argument("--limit", type=int, default=50, help="rows to print (0 = all)")

    p = sub.add_parser("get", help="download a subset to NetCDF")
    _add_facets(p, required=True)
    p.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    p.add_argument("--region", help="region name used in file names")
    p.add_argument("--time", nargs="+", metavar="DATE", help="one date or START END (YYYY[-MM[-DD]])")
    p.add_argument("--levels", nargs="+", type=float, help="levels to keep (nearest match)")
    p.add_argument("--level-range", nargs=2, type=float, metavar=("MIN", "MAX"))
    p.add_argument("--dest", default=DEFAULT_DEST, help="output path template")
    p.add_argument("--split", choices=["year", "month", "all"], default="year")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--max-task-mb", type=float, default=1024)
    p.add_argument("--compress", type=int, default=1, help="zlib level, 0 to disable")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="list the files without downloading")

    p = sub.add_parser("update-catalog", help="refresh the cached catalogue")
    p.add_argument("--mirror", choices=["gcs", "aws"], default="gcs")
    return parser


def _member(value):
    return "all" if value == ["all"] else value


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s"
    )

    if args.command == "update-catalog":
        df = load_catalog(args.mirror, refresh=True)
        print(f"{len(df)} stores in the {args.mirror} catalogue")
        return 0

    if args.command == "search":
        member = _member(args.member)
        df = search(
            mirror=args.mirror,
            variable_id=args.variable,
            source_id=args.source,
            experiment_id=args.experiment,
            table_id=args.table,
            member_id=None if member == "all" else member,
            grid_label=args.grid,
        )
        cols = ["source_id", "experiment_id", "member_id", "table_id", "variable_id", "grid_label", "version"]
        shown = df[cols] if args.limit == 0 else df[cols].head(args.limit)
        if len(shown):
            print(shown.to_string(index=False))
        print(f"\n{len(df)} dataset(s) found" + (f", {len(shown)} shown" if len(shown) < len(df) else ""))
        return 0

    if args.time is not None and len(args.time) > 2:
        print("--time takes one date or START END", file=sys.stderr)
        return 2
    time = None if args.time is None else slice(args.time[0], args.time[-1])
    try:
        dl = CMIP6Downloader(
            args.variable,
            source_id=args.source,
            experiment_id=args.experiment,
            table_id=args.table,
            member_id=_member(args.member),
            grid_label=args.grid,
            bbox_WSEN=args.bbox,
            region_name=args.region,
            levels=args.levels,
            level_range=args.level_range,
            dest_path=args.dest,
            split=args.split,
            mirror=args.mirror,
            max_workers=args.workers,
            max_task_mb=args.max_task_mb,
            compress=args.compress,
            overwrite=args.overwrite,
        )
    except LookupError as err:
        print(err, file=sys.stderr)
        return 1
    paths = dl.download(time, dry_run=args.dry_run)
    if args.dry_run:
        print("\n".join(str(p) for p in paths))
    print(f"{len(paths)} file(s)" + (" planned" if args.dry_run else " ready"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
