"""Parallel, resumable download of subsetted CMIP data to NetCDF files."""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from . import catalog as cat
from . import esgf
from .subset import parse_time, subset_bbox, subset_levels, time_index_range, time_keys

log = logging.getLogger(__name__)

DEFAULT_DEST = (
    "cmip-data/{mip_era}/{source_id}/{experiment_id}/{table_id}/{variable_id}/"
    "{variable_id}_{table_id}_{source_id}_{experiment_id}_{member_id}_{grid_label}"
    "_{region_name}_{period}.nc"
)
_KEEP_ENCODING = ("dtype", "_FillValue", "missing_value", "units", "calendar")
_WRITE_LOCK = threading.Lock()  # the HDF5 library is not thread-safe


@dataclass
class _File:
    path: Path
    start: int  # half-open index range along the store's time axis
    stop: int


@dataclass
class _Task:
    """One load from the cloud, written out as one or more files."""

    facets: dict
    ds: xr.Dataset
    files: list[_File] = field(default_factory=list)


def open_store(zstore: str, storage_options: dict | None = None) -> xr.Dataset:
    """Open one CMIP6 zarr store lazily, with cftime dates for any calendar."""
    kwargs = {
        "chunks": {},
        "decode_times": xr.coders.CFDatetimeCoder(use_cftime=True),
        # Credentials only make sense for remote URLs, not for local paths.
        "storage_options": (storage_options or None) if "://" in str(zstore) else None,
    }
    try:
        return xr.open_zarr(zstore, consolidated=True, **kwargs)
    except (KeyError, ValueError, FileNotFoundError):
        return xr.open_zarr(zstore, consolidated=False, **kwargs)


def split_periods(keys: np.ndarray, start: int, stop: int, split: str):
    """Split time steps [start, stop) into (label, start, stop) output periods."""
    sel = keys[start:stop]
    if split == "all":
        return [(f"{sel[0] // 100}-{sel[-1] // 100}", start, stop)]
    group = sel // 10000 if split == "year" else sel // 100
    labels, first = np.unique(group, return_index=True)
    order = np.argsort(first)
    labels, first = labels[order], first[order]
    last = np.append(first[1:], sel.size)
    return [(str(int(lb)), start + int(a), start + int(b)) for lb, a, b in zip(labels, first, last)]


def group_by_chunk(files: list[_File], chunk_edges: np.ndarray, steps_per_task: int):
    """Group output files that read the same source chunks into one load.

    CMIP6 zarr stores are chunked along time, often with decades of monthly
    data in a single chunk. Loading each yearly file on its own would fetch
    that chunk once per year; grouping fetches it once. ``steps_per_task``
    bounds the memory of one group.
    """
    groups: list[list[_File]] = []
    for f in sorted(files, key=lambda f: f.start):
        if groups:
            cur = groups[-1]
            shares = np.searchsorted(chunk_edges, cur[-1].stop - 1, side="right") == (
                np.searchsorted(chunk_edges, f.start, side="right")
            )
            contiguous = cur[-1].stop == f.start
            if shares and contiguous and (f.stop - cur[0].start) <= steps_per_task:
                cur.append(f)
                continue
        groups.append([f])
    return groups


def _main_var(ds: xr.Dataset, variable: str) -> str:
    """Name of the data variable for a variable id (they differ in a few datasets)."""
    if variable in ds.data_vars:
        return variable
    candidates = [n for n in ds.data_vars if not n.endswith(("_bnds", "_bounds")) and ds[n].ndim >= 2]
    if not candidates:
        raise KeyError(f"Variable {variable!r} not found in dataset")
    return max(candidates, key=lambda n: ds[n].ndim)


class CMIPDownloader:
    """Download regional, time-limited subsets of CMIP3, CMIP5, CMIP6 and CMIP7.

    CMIP6 is read from the cloud mirrors (zarr on Google Cloud or AWS), which
    is the fastest route. The other phases, and CMIP6 datasets that were never
    mirrored, are fetched from ESGF through ``intake-esgf``. Facets are always
    given with their CMIP6 names and translated for the phase.

    Parameters
    ----------
    variables : variable id(s), e.g. ``"tos"`` or ``["thetao", "so"]``.
    mip_era : "CMIP6" (default), "CMIP7", "CMIP5" or "CMIP3".
    source : "auto" (default), "cloud" or "esgf". "auto" uses the cloud for
        CMIP6 and falls back to ESGF when the cloud has no match.
    source_id, experiment_id, table_id, member_id, grid_label, activity_id,
    institution_id : facet filters, each a string or list of strings.
        With ``member_id=None`` one member per model run is used (r1i1p1f1
        when it exists); pass ``member_id="all"`` for every member. With
        ``grid_label=None`` the native grid is preferred.
    frequency : output frequency for the phases without MIP tables (CMIP7,
        CMIP3) or as an alternative to ``table_id`` in CMIP5.
    extra_facets : further ESGF search terms in the phase's own vocabulary,
        e.g. ``{"realm": "ocean"}`` or ``{"variable_branding_suffix": ...}``.
    bbox_WSEN : (west, south, east, north) in degrees, or None for global.
    region_name : label inserted in file names.
    levels : vertical levels to keep (nearest match, in the dataset's units).
    level_range : (min, max) of the vertical coordinate to keep.
    dest_path : template for output files. Available fields are ``mip_era``,
        the facets, ``region_name`` and ``period``.
    split : "year" (default), "month" or "all" – time span of each file.
    mirror : cloud mirror, "gcs" (Google Cloud) or "aws".
    max_workers : number of parallel loads/downloads.
    max_task_mb : upper bound on the uncompressed size of one load.
    compress : zlib level for the NetCDF output (0 disables compression).
    raw_cache : directory for the global files downloaded from ESGF
        (default: intake-esgf's own cache, ``~/.esgf``).
    keep_raw : keep those global files after subsetting (default True, so a
        later request for another region needs no new download).
    catalog : a pre-loaded or custom cloud catalogue DataFrame (optional).
    """

    def __init__(
        self,
        variables,
        *,
        mip_era: str = "CMIP6",
        source: str = "auto",
        source_id=None,
        experiment_id=None,
        table_id=None,
        member_id=None,
        grid_label=None,
        activity_id=None,
        institution_id=None,
        frequency=None,
        extra_facets: dict | None = None,
        bbox_WSEN=None,
        region_name=None,
        levels=None,
        level_range=None,
        dest_path: str = DEFAULT_DEST,
        split: str = "year",
        mirror: str = "gcs",
        max_workers: int = 4,
        max_task_mb: float = 1024,
        compress: int = 1,
        raw_cache=None,
        keep_raw: bool = True,
        catalog: pd.DataFrame | None = None,
        storage_options: dict | None = None,
        overwrite: bool = False,
        retries: int = 3,
        esgf_catalog=None,
    ):
        if split not in ("year", "month", "all"):
            raise ValueError("split must be 'year', 'month' or 'all'")
        if source not in ("auto", "cloud", "esgf"):
            raise ValueError("source must be 'auto', 'cloud' or 'esgf'")
        self.mip_era = esgf.check_phase(mip_era)
        if source == "cloud" and self.mip_era != "CMIP6":
            raise ValueError(f"The cloud mirrors hold CMIP6 only; use source='esgf' for {self.mip_era}")
        self.bbox = None if bbox_WSEN is None else tuple(float(v) for v in bbox_WSEN)
        self.region_name = region_name or ("global" if self.bbox is None else "region")
        self.levels = levels
        self.level_range = level_range
        self.dest_path = str(dest_path)
        self.split = split
        self.max_workers = max(1, int(max_workers))
        self.max_task_mb = float(max_task_mb)
        self.compress = int(compress)
        self.keep_raw = keep_raw
        self.overwrite = overwrite
        self.retries = max(1, int(retries))
        if storage_options is not None:
            self.storage_options = storage_options
        elif catalog is None:
            self.storage_options = cat.storage_options(mirror)
        else:
            self.storage_options = None

        all_members = isinstance(member_id, str) and member_id == "all"
        member = None if all_members else member_id
        pick = {"member_id": "all" if all_members else member_id, "grid_label": grid_label}
        self._esgf = None
        self._opened: dict = {}
        self._raw: dict = {}
        self._last_plan: list = []

        esgf_only = frequency is not None or bool(extra_facets) or esgf_catalog is not None
        use_cloud = source == "cloud" or (source == "auto" and self.mip_era == "CMIP6" and not esgf_only)
        datasets = pd.DataFrame()
        if use_cloud:
            if frequency is not None or extra_facets:
                raise ValueError("frequency and extra_facets apply to source='esgf' only")
            found = cat.search(
                catalog,
                mirror=mirror,
                variable_id=variables,
                source_id=source_id,
                experiment_id=experiment_id,
                table_id=table_id,
                member_id=member,
                grid_label=grid_label,
                activity_id=activity_id,
                institution_id=institution_id,
            )
            datasets = cat.select_default(found, **pick).assign(mip_era="CMIP6")
            if datasets.empty and source == "auto" and catalog is None:
                log.info("No match on the cloud mirror, searching ESGF instead")
                use_cloud = False
        if not use_cloud:
            self._esgf = esgf.ESGFSource(
                self.mip_era,
                extra_facets=extra_facets,
                raw_cache=raw_cache,
                num_threads=self.max_workers,
                catalog=esgf_catalog,
                variable_id=variables,
                source_id=source_id,
                experiment_id=experiment_id,
                table_id=table_id,
                member_id=member,
                grid_label=grid_label,
                activity_id=activity_id,
                institution_id=institution_id,
                frequency=frequency,
            )
            datasets = cat.select_default(self._esgf.datasets, **pick)
            if not datasets.empty:
                self._esgf.restrict(datasets)
        if datasets.empty:
            raise LookupError(
                f"No {self.mip_era} dataset matches the request. Use cmip_downloader.search() "
                "to see which facet values are available."
            )
        self.datasets = datasets
        self.source = "esgf" if self._esgf is not None else "cloud"

    def __repr__(self):
        return (
            f"<CMIPDownloader: {len(self.datasets)} {self.mip_era} dataset(s) from {self.source}, "
            f"region={self.region_name!r}, bbox={self.bbox}>"
        )

    @property
    def _workers(self) -> int:
        """Threads used to open, load and write datasets.

        Cloud stores are read over the network and gain from parallel loads.
        ESGF data is subset from local NetCDF files, and the HDF5 library
        underneath is not thread-safe, so those are handled one at a time
        (the ESGF downloads themselves still run in parallel).
        """
        return 1 if self._esgf is not None else self.max_workers

    # ------------------------------------------------------------------ planning
    def _subset(self, key, opener, variable: str) -> xr.Dataset:
        """Open a dataset and apply the spatial and vertical selection (lazy)."""
        if key not in self._opened:
            ds = opener()
            if self.bbox is not None:
                ds = subset_bbox(ds, self.bbox)
            ds = subset_levels(ds, _main_var(ds, variable), self.levels, self.level_range)
            self._opened[key] = ds
        return self._opened[key]

    def _path(self, facets: dict, period: str) -> Path:
        fields = {k: v for k, v in facets.items() if not k.startswith("_")}
        return Path(self.dest_path.format(**fields, region_name=self.region_name, period=period))

    def _plan_dataset(self, row: dict, start, stop) -> tuple[list[_Task], list[Path]]:
        facets = {k: row[k] for k in ("mip_era", *cat.FACETS)}
        if self._esgf is None:
            zstore = row["zstore"]
            ds = self._subset(zstore, lambda: open_store(zstore, self.storage_options), facets["variable_id"])
        else:
            raw = self._raw.get(row["_row"])
            if not raw:
                log.warning("%s: no files in the requested period", ".".join(str(v) for v in facets.values()))
                return [], []
            ds = self._subset(tuple(raw), lambda: esgf.open_files(raw), facets["variable_id"])
        var = _main_var(ds, facets["variable_id"])
        facets["_var"] = var

        if "time" not in ds[var].dims:  # fixed fields (fx, Ofx)
            files = [_File(self._path(facets, "fx"), 0, 0)]
            groups = [files]
        else:
            keys = time_keys(ds)
            a, b = time_index_range(keys, start, stop)
            if a == b:
                log.warning("%s: no data in the requested period", ".".join(str(v) for v in facets.values()))
                return [], []
            files = [
                _File(self._path(facets, label), i, j)
                for label, i, j in split_periods(keys, a, b, self.split)
            ]
            chunks = ds[var].chunksizes.get("time") or (ds.sizes["time"],)
            step_bytes = ds[var].isel(time=0).nbytes or 1
            steps = max(1, int(self.max_task_mb * 2**20 // step_bytes))
            pending = [f for f in files if self.overwrite or not f.path.exists()]
            groups = group_by_chunk(pending, np.cumsum(chunks), steps)

        paths = [f.path for f in files]
        tasks = [
            _Task(facets, ds, g)
            for g in groups
            if any(self.overwrite or not f.path.exists() for f in g)
        ]
        return tasks, paths

    def plan(self, time=None) -> tuple[list[_Task], list[Path]]:
        """Work out the loads to run and the complete list of output files.

        For ESGF this downloads the source files first, because the periods
        they cover are only known once they are on disk.
        """
        start, stop = parse_time(time)
        if self._esgf is not None:
            self._raw = self._esgf.fetch(start, stop)
        rows = self.datasets.to_dict("records")
        tasks: list[_Task] = []
        paths: list[Path] = []
        self._last_plan = []
        # Opening a cloud store is a few small requests; do all of them at once.
        with ThreadPoolExecutor(self._workers) as pool:
            for row, (t, p) in zip(rows, pool.map(lambda r: self._plan_dataset(r, start, stop), rows)):
                tasks += t
                paths += p
                self._last_plan.append((row, p))
        return tasks, paths

    # ---------------------------------------------------------------- execution
    def _encoding(self, ds: xr.Dataset) -> dict:
        enc = {}
        for name, var in ds.variables.items():
            e = {k: v for k, v in var.encoding.items() if k in _KEEP_ENCODING}
            if self.compress and name in ds.data_vars and var.ndim >= 2 and var.dtype.kind == "f":
                e.update(zlib=True, complevel=self.compress, shuffle=True)
            enc[name] = e
        return enc

    def _write(self, ds: xr.Dataset, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        for var in ds.variables.values():  # drop zarr chunking/compressor settings
            var.encoding = {k: v for k, v in var.encoding.items() if k in _KEEP_ENCODING}
        with _WRITE_LOCK:
            try:
                ds.to_netcdf(tmp, encoding=self._encoding(ds))
                os.replace(tmp, path)  # a file exists only once it is complete
            finally:
                tmp.unlink(missing_ok=True)

    def _run(self, task: _Task) -> list[Path]:
        ds = task.ds
        timed = "time" in ds[task.facets["_var"]].dims
        first, last = task.files[0].start, task.files[-1].stop
        lazy = ds.isel(time=slice(first, last)) if timed else ds
        for attempt in range(1, self.retries + 1):
            try:
                loaded = lazy.load(scheduler="threads")
                break
            except Exception as err:  # network hiccups: back off and retry
                if attempt == self.retries:
                    raise
                log.warning("Load failed (%s), retry %d/%d", err, attempt, self.retries - 1)
                _time.sleep(2**attempt)
        written = []
        for f in task.files:
            if f.path.exists() and not self.overwrite:
                continue
            part = loaded.isel(time=slice(f.start - first, f.stop - first)) if timed else loaded
            self._write(part, f.path)
            written.append(f.path)
        return written

    def download(self, time=None, dry_run: bool = False) -> list[Path]:
        """Download the requested period and return the paths of all its files.

        ``time`` is ``None`` (everything), a string such as ``"2015"`` or
        ``"2015-06"``, or ``slice("2015", "2100")``. Files already on disk are
        skipped, so an interrupted download resumes where it stopped.
        """
        if dry_run and self._esgf is not None:
            log.info(
                "%d dataset(s) would be fetched from ESGF; their output files are known "
                "only once the source files are downloaded",
                len(self.datasets),
            )
            return []
        tasks, paths = self.plan(time)
        todo = sum(len(t.files) for t in tasks)
        log.info("%d file(s) requested, %d to download in %d load(s)", len(paths), todo, len(tasks))
        if dry_run:
            return paths
        done = 0
        with ThreadPoolExecutor(self._workers) as pool:
            futures = {pool.submit(self._run, t): t for t in tasks}
            for fut in as_completed(futures):
                for path in fut.result():
                    done += 1
                    log.info("[%d/%d] %s", done, todo, path)
        if self._esgf is not None and not self.keep_raw:
            for key in [k for k in self._opened if isinstance(k, tuple)]:
                self._opened.pop(key).close()
            esgf.remove_raw([p for files in self._raw.values() for p in files])
        return paths

    def fetch_local(self, time=None):
        """Return the data as xarray, downloading whatever is not yet on disk.

        A single matching dataset gives an ``xarray.Dataset``; several give a
        dict keyed by ``source_id.experiment_id.member_id.table_id.variable_id``.
        """
        self.download(time)
        coder = xr.coders.CFDatetimeCoder(use_cftime=True)
        out = {}
        for row, paths in self._last_plan:
            if not paths:
                continue
            key = ".".join(
                row[k]
                for k in ("source_id", "experiment_id", "member_id", "table_id", "variable_id")
            )
            if len(paths) == 1:
                out[key] = xr.open_dataset(paths[0], decode_times=coder)
            else:
                out[key] = xr.open_mfdataset(
                    paths, combine="nested", concat_dim="time", decode_times=coder,
                    data_vars="minimal", coords="minimal", compat="override",
                )
        return next(iter(out.values())) if len(out) == 1 else out


CMIP6Downloader = CMIPDownloader  # name used by version 0.1
