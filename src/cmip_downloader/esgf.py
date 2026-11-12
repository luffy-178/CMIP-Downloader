"""ESGF back end: every CMIP phase, through the official ``intake-esgf`` client.

The cloud mirrors only hold CMIP6. CMIP3, CMIP5 and CMIP7 (and the parts of
CMIP6 that were never mirrored) live on the Earth System Grid Federation.
ESGF serves whole NetCDF files, so this back end downloads the files that
overlap the requested period (in parallel, with checksum verification and a
local cache, all handled by ``intake-esgf``) and the downloader then cuts
them to the requested region, levels and period.

Facet names differ between phases; users give the CMIP6 names and they are
translated here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import xarray as xr

from .catalog import FACETS

log = logging.getLogger(__name__)

PHASES = ("CMIP3", "CMIP5", "CMIP6", "CMIP7")
MISSING = "na"

# unified (CMIP6-style) name -> name used by each phase on ESGF
NATIVE = {
    "CMIP6": {
        "variable_id": "variable_id",
        "source_id": "source_id",
        "experiment_id": "experiment_id",
        "member_id": "member_id",
        "table_id": "table_id",
        "grid_label": "grid_label",
        "activity_id": "activity_id",
        "institution_id": "institution_id",
    },
    "CMIP7": {
        "variable_id": "variable_id",
        "source_id": "source_id",
        "experiment_id": "experiment_id",
        "member_id": "variant_label",
        "frequency": "frequency",
        "grid_label": "grid_label",
        "activity_id": "activity_id",
        "institution_id": "institution_id",
    },
    "CMIP5": {
        "variable_id": "variable",
        "source_id": "model",
        "experiment_id": "experiment",
        "member_id": "ensemble",
        "table_id": "cmor_table",
        "frequency": "time_frequency",
        "institution_id": "institute",
    },
    "CMIP3": {
        "variable_id": "variable",
        "source_id": "model",
        "experiment_id": "experiment",
        "member_id": "ensemble",
        "frequency": "time_frequency",
        "institution_id": "institute",
    },
}

_HINTS = {
    ("CMIP7", "table_id"): "CMIP7 has no MIP tables; use frequency='mon' (or 'day', ...) and, "
    "if needed, extra_facets={'variable_branding_suffix': ..., 'region': ...}",
    ("CMIP3", "table_id"): "CMIP3 has no MIP tables; use frequency='mo' and extra_facets={'realm': 'ocean'}",
    ("CMIP6", "frequency"): "for CMIP6 select the frequency through table_id (Omon, Amon, day, ...)",
    ("CMIP5", "grid_label"): "CMIP5 has no grid labels",
    ("CMIP3", "grid_label"): "CMIP3 has no grid labels",
}


def check_phase(mip_era: str) -> str:
    phase = str(mip_era).upper()
    if phase not in PHASES:
        raise ValueError(f"mip_era must be one of {PHASES}, got {mip_era!r}")
    return phase


def translate_facets(mip_era: str, extra_facets: dict | None = None, **facets) -> dict:
    """Turn CMIP6-style facet names into the ESGF search terms of a phase."""
    phase = check_phase(mip_era)
    names = NATIVE[phase]
    out = {"project": phase}
    for name, value in facets.items():
        if value is None:
            continue
        if name not in names:
            hint = _HINTS.get((phase, name), f"valid facets are {sorted(names)}")
            raise ValueError(f"{name!r} is not a facet of {phase}: {hint}")
        out[names[name]] = value
    out.update(extra_facets or {})
    return out


def normalise(mip_era: str, df: pd.DataFrame) -> pd.DataFrame:
    """Give an ESGF search result the same columns as the cloud catalogue.

    Facets a phase does not have are filled with "na". CMIP3 and CMIP7 have no
    MIP table, so ``table_id`` holds the frequency instead; for CMIP7 the
    branding suffix and region are appended because they distinguish datasets
    of the same variable and frequency.
    """
    phase = check_phase(mip_era)
    names = NATIVE[phase]
    out = pd.DataFrame(index=df.index)
    out["mip_era"] = phase
    for facet in FACETS:
        native = names.get(facet)
        out[facet] = df[native].astype(str) if native in df.columns else MISSING
    if "table_id" not in names:
        parts = [c for c in (names["frequency"], "variable_branding_suffix", "region") if c in df.columns]
        if parts:
            out["table_id"] = df[parts].astype(str).agg("_".join, axis=1)
    version = df["version"] if "version" in df.columns else 0
    out["version"] = pd.to_numeric(
        pd.Series(version, index=df.index).astype(str).str.lstrip("v"), errors="coerce"
    ).fillna(0)
    out["_row"] = df.index
    out = out.sort_values("version").drop_duplicates(list(FACETS), keep="last")
    return out.sort_values(list(FACETS)).reset_index(drop=True)


def _new_catalog(raw_cache=None, num_threads=None):
    try:
        import intake_esgf
    except ImportError as err:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "The ESGF back end needs intake-esgf. Install it with: pip install intake-esgf"
        ) from err
    settings = {}
    if raw_cache is not None:
        settings["local_cache"] = [str(raw_cache)]
    if num_threads is not None:
        settings["num_threads"] = int(num_threads)
    if settings:
        intake_esgf.conf.set(**settings)
    return intake_esgf.ESGFCatalog()


def _timestamp(key: int | None):
    """YYYYMMDD -> pandas Timestamp, or None when outside the range pandas supports."""
    if key is None:
        return None
    y, m, d = key // 10000, key // 100 % 100, min(key % 100, 28)
    try:
        return pd.Timestamp(year=y, month=m, day=d)
    except (ValueError, pd.errors.OutOfBoundsDatetime):
        return None


class ESGFSource:
    """A search on ESGF plus the means to fetch and open its datasets."""

    def __init__(
        self,
        mip_era: str,
        *,
        extra_facets: dict | None = None,
        raw_cache=None,
        num_threads: int | None = None,
        quiet: bool = True,
        catalog=None,
        **facets,
    ):
        self.mip_era = check_phase(mip_era)
        self.quiet = quiet
        terms = translate_facets(self.mip_era, extra_facets, **facets)
        self.catalog = catalog if catalog is not None else _new_catalog(raw_cache, num_threads)
        try:
            self.catalog.search(quiet=quiet, **terms)
        except Exception as err:
            if type(err).__name__ != "NoSearchResults":
                raise
            self.datasets = pd.DataFrame(columns=["mip_era", *FACETS, "version", "_row"])
            return
        self.datasets = normalise(self.mip_era, self.catalog.df)

    def restrict(self, datasets: pd.DataFrame) -> None:
        """Keep only the selected datasets, so nothing else is downloaded."""
        self.datasets = datasets.reset_index(drop=True)
        self.catalog.df = self.catalog.df.loc[list(datasets["_row"])]

    def fetch(self, start: int | None = None, stop: int | None = None) -> dict:
        """Download the files overlapping [start, stop]; return {_row: [paths]}."""
        self.catalog.file_start = _timestamp(start)
        self.catalog.file_end = _timestamp(stop)
        paths = self.catalog.to_path_dict(minimal_keys=False, separator="|", quiet=self.quiet)
        by_key = dict(zip(self.catalog.df["key"], self.catalog.df.index))
        return {by_key[key]: sorted(Path(p) for p in files) for key, files in paths.items()}


def open_files(paths) -> xr.Dataset:
    """Open the NetCDF files of one dataset lazily as a single dataset."""
    coder = xr.coders.CFDatetimeCoder(use_cftime=True)
    if len(paths) == 1:
        return xr.open_dataset(paths[0], chunks={}, decode_times=coder)
    return xr.open_mfdataset(
        paths,
        combine="by_coords",
        decode_times=coder,
        data_vars="minimal",
        coords="minimal",
        compat="override",
        chunks={},
    )


def remove_raw(paths) -> None:
    """Delete downloaded source files and any directories left empty."""
    for path in paths:
        Path(path).unlink(missing_ok=True)
    for parent in {Path(p).parent for p in paths}:
        try:
            parent.rmdir()
        except OSError:
            pass


__all__ = ["PHASES", "ESGFSource", "normalise", "open_files", "remove_raw", "translate_facets"]
