"""Locate CMIP data: the Pangeo catalogue of the CMIP6 cloud mirrors, or ESGF.

The catalogue is one CSV file per mirror with a row per zarr store. It is
downloaded once, cached on disk and refreshed when it gets old, so a search
costs a local file read instead of a round of ESGF queries.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

MIRRORS = {
    "gcs": {
        "catalog": "https://storage.googleapis.com/cmip6/pangeo-cmip6.csv",
        "storage_options": {"token": "anon"},
    },
    "aws": {
        "catalog": "https://cmip6-pds.s3.amazonaws.com/pangeo-cmip6.csv",
        "storage_options": {"anon": True},
    },
}

FACETS = (
    "activity_id",
    "institution_id",
    "source_id",
    "experiment_id",
    "member_id",
    "table_id",
    "variable_id",
    "grid_label",
)

GRID_PREFERENCE = ("gn", "gr", "gr1", "gr2", "gm")
_MEMBER_RE = re.compile(r"(?:s(\d+)-)?r(\d+)i(\d+)p(\d+)(?:f(\d+))?")


def cache_dir() -> Path:
    """Directory holding the cached catalogue (override with CMIP_DOWNLOADER_CACHE)."""
    root = os.environ.get("CMIP_DOWNLOADER_CACHE")
    path = Path(root) if root else Path.home() / ".cache" / "cmip_downloader"
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_options(mirror: str) -> dict:
    return dict(MIRRORS[mirror]["storage_options"])


def _check_mirror(mirror: str) -> None:
    if mirror not in MIRRORS:
        raise ValueError(f"mirror must be one of {sorted(MIRRORS)}, got {mirror!r}")


def load_catalog(
    mirror: str = "gcs", refresh: bool = False, max_age_days: float = 7.0
) -> pd.DataFrame:
    """Return the catalogue of a mirror as a DataFrame, downloading it if needed.

    Parameters
    ----------
    mirror : "gcs" (Google Cloud) or "aws" (Amazon S3).
    refresh : force a new download of the catalogue.
    max_age_days : re-download when the cached copy is older than this.
    """
    _check_mirror(mirror)
    path = cache_dir() / f"pangeo-cmip6-{mirror}.csv"
    stale = (
        refresh
        or not path.exists()
        or (time.time() - path.stat().st_mtime) > max_age_days * 86400
    )
    if stale:
        url = MIRRORS[mirror]["catalog"]
        log.info("Downloading CMIP6 catalogue from %s", url)
        tmp = path.with_suffix(".part")
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as out:
                shutil.copyfileobj(resp, out, length=1 << 20)
            os.replace(tmp, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            if not path.exists():
                raise
            log.warning("Catalogue refresh failed, using the cached copy at %s", path)
    return pd.read_csv(path, low_memory=False)


def _as_list(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value]
    return [str(value)]


def member_sort_key(member: str) -> tuple:
    """Sort r1i1p1f1 before r2i1p1f1 before r10i1p1f1 (also CMIP5 style, r1i1p1)."""
    m = _MEMBER_RE.fullmatch(str(member))
    if not m:
        return (1, 0, 0, 0, 0, 0, str(member))
    s, r, i, p, f = (int(g) if g else 0 for g in m.groups())
    return (0, s, i, p, f, r, str(member))


def _grid_rank(grid: str) -> int:
    return GRID_PREFERENCE.index(grid) if grid in GRID_PREFERENCE else len(GRID_PREFERENCE)


def search(
    catalog: pd.DataFrame | None = None,
    *,
    mip_era: str = "CMIP6",
    source: str = "auto",
    mirror: str = "gcs",
    latest_only: bool = True,
    frequency=None,
    extra_facets: dict | None = None,
    **facets,
) -> pd.DataFrame:
    """List the datasets matching the facet values.

    Every facet accepts a string or a list of strings, for example
    ``search(variable_id=["tos", "thetao"], source_id="CESM2",
    experiment_id="ssp585", table_id="Omon")``.

    CMIP6 is searched in the cloud catalogue unless ``source="esgf"``; the
    other phases are searched on ESGF, e.g.
    ``search(mip_era="CMIP5", variable_id="tos", experiment_id="rcp85")``.
    """
    unknown = set(facets) - set(FACETS)
    if unknown:
        raise TypeError(f"Unknown facet(s) {sorted(unknown)}; valid facets are {FACETS}")
    if source == "esgf" or str(mip_era).upper() != "CMIP6" or frequency is not None or extra_facets:
        from .esgf import ESGFSource

        found = ESGFSource(mip_era, extra_facets=extra_facets, frequency=frequency, **facets)
        return found.datasets.drop(columns="_row")
    df = load_catalog(mirror) if catalog is None else catalog
    mask = pd.Series(True, index=df.index)
    for name, value in facets.items():
        values = _as_list(value)
        if values is not None:
            mask &= df[name].isin(values)
    out = df[mask]
    if latest_only and len(out):
        out = out.sort_values("version").drop_duplicates(list(FACETS), keep="last")
    return out.sort_values(list(FACETS)).reset_index(drop=True)


def select_default(df: pd.DataFrame, member_id=None, grid_label=None) -> pd.DataFrame:
    """Reduce a search result to one member and one grid per model run.

    Applied only to the facets the user left open: with no ``member_id`` the
    first member of each model/experiment is kept, with no ``grid_label`` the
    native grid is preferred over regridded output.
    """
    if df.empty:
        return df
    out = df
    if grid_label is None:
        keys = [f for f in FACETS if f != "grid_label"]
        out = (
            out.assign(_rank=out["grid_label"].map(_grid_rank))
            .sort_values("_rank")
            .drop_duplicates(keys, keep="first")
            .drop(columns="_rank")
        )
    if member_id is None:
        # Keep, per model run, the member that covers the most requested
        # variables; ties go to the lowest realisation (r1i1p1f1 first).
        keys = ["source_id", "experiment_id"]
        counts = out.groupby(keys + ["member_id"], as_index=False).size()
        counts["_key"] = counts["member_id"].map(member_sort_key)
        first = (
            counts.sort_values(["size", "_key"], ascending=[False, True])
            .drop_duplicates(keys, keep="first")[keys + ["member_id"]]
        )
        out = out.merge(first, on=keys + ["member_id"], how="inner")
    return out.sort_values(list(FACETS)).reset_index(drop=True)
