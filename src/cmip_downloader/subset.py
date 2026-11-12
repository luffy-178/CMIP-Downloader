"""Lazy subsetting of CMIP6 datasets in space, depth/pressure and time.

Everything here works on index selections of a dask-backed dataset, so no
data is transferred until the result is loaded.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

LAT_NAMES = ("lat", "latitude", "nav_lat")
LON_NAMES = ("lon", "longitude", "nav_lon")
LEVEL_NAMES = ("lev", "plev", "olevel", "depth", "deptht", "level", "sdepth")


def _find(ds: xr.Dataset, names: tuple[str, ...], standard_name: str) -> str | None:
    for name in names:
        if name in ds.variables:
            return name
    for name, var in ds.variables.items():
        if var.attrs.get("standard_name") == standard_name:
            return name
    return None


def find_lat_lon(ds: xr.Dataset) -> tuple[str, str]:
    lat = _find(ds, LAT_NAMES, "latitude")
    lon = _find(ds, LON_NAMES, "longitude")
    if lat is None or lon is None:
        raise ValueError("Could not identify latitude/longitude coordinates in the dataset")
    return lat, lon


def find_level(ds: xr.Dataset, variable: str | None = None) -> str | None:
    dims = ds[variable].dims if variable else ds.dims
    for name in LEVEL_NAMES:
        if name in dims:
            return name
    return None


def _lon_offset(lon, west: float, east: float):
    """Eastward distance of each longitude from ``west`` and the box width.

    Working with distances modulo 360 makes the test independent of the
    grid convention (0..360 or -180..180) and of boxes crossing the seam.
    """
    width = east - west
    width = 360.0 if width >= 360 else width % 360
    return (np.asarray(lon, dtype="float64") - west) % 360, width


def subset_bbox(ds: xr.Dataset, bbox_WSEN) -> xr.Dataset:
    """Cut a dataset to a (west, south, east, north) box in degrees.

    Handles regular grids (1-D lat/lon), curvilinear ocean grids (2-D lat/lon,
    where the smallest index rectangle containing the box is returned) and
    unstructured grids (lat/lon sharing a single cell dimension).
    """
    west, south, east, north = (float(v) for v in bbox_WSEN)
    if south > north:
        raise ValueError("bbox_WSEN: south must not exceed north")
    lat_name, lon_name = find_lat_lon(ds)
    lat = ds[lat_name]
    lon = ds[lon_name]
    lat_v = np.asarray(lat.values, dtype="float64")
    off, width = _lon_offset(lon.values, west, east)
    lat_ok = (lat_v >= south) & (lat_v <= north)
    lon_ok = off <= width

    if lat.ndim == 1 and lon.ndim == 1 and lat.dims != lon.dims:
        ilat = np.flatnonzero(lat_ok)
        ilon = np.flatnonzero(lon_ok)
        if not ilat.size or not ilon.size:
            raise ValueError(f"No grid points inside bbox {tuple(bbox_WSEN)}")
        # Order longitudes eastward from the western edge so a box crossing
        # the grid seam comes out contiguous.
        ilon = ilon[np.argsort(off[ilon], kind="stable")]
        return ds.isel({lat.dims[0]: _as_indexer(ilat), lon.dims[0]: _as_indexer(ilon)})

    if lat.dims != lon.dims:
        raise ValueError("Latitude and longitude have incompatible dimensions")

    mask = lat_ok & lon_ok
    if not mask.any():
        raise ValueError(f"No grid points inside bbox {tuple(bbox_WSEN)}")
    if lat.ndim == 1:  # unstructured: one cell dimension
        return ds.isel({lat.dims[0]: _as_indexer(np.flatnonzero(mask))})
    if lat.ndim == 2:
        indexers = {}
        for axis, dim in enumerate(lat.dims):
            hit = np.flatnonzero(mask.any(axis=1 - axis))
            indexers[dim] = slice(int(hit[0]), int(hit[-1]) + 1)
        return ds.isel(indexers)
    raise ValueError(f"Unsupported latitude/longitude layout with {lat.ndim} dimensions")


def _as_indexer(idx: np.ndarray):
    """Use a slice when indices are contiguous: cheaper for dask than fancy indexing."""
    if idx.size and np.array_equal(idx, np.arange(idx[0], idx[0] + idx.size)):
        return slice(int(idx[0]), int(idx[-1]) + 1)
    return idx


def subset_levels(ds, variable=None, levels=None, level_range=None) -> xr.Dataset:
    """Select vertical levels by nearest value and/or by a (min, max) range.

    Values are in the units of the dataset's own vertical coordinate
    (Pa for ``plev``; metres for most ocean models, centimetres for some).
    Datasets without a vertical dimension are returned unchanged.
    """
    if levels is None and level_range is None:
        return ds
    name = find_level(ds, variable)
    if name is None:
        return ds
    coord = np.asarray(ds[name].values, dtype="float64")
    if level_range is not None:
        lo, hi = sorted(float(v) for v in level_range)
        idx = np.flatnonzero((coord >= lo) & (coord <= hi))
        if not idx.size:
            raise ValueError(f"No levels of {name!r} inside range {(lo, hi)}")
    else:
        idx = np.arange(coord.size)
    if levels is not None:
        wanted = np.atleast_1d(np.asarray(levels, dtype="float64"))
        nearest = np.abs(coord[idx][None, :] - wanted[:, None]).argmin(axis=1)
        idx = idx[np.unique(nearest)]
    return ds.isel({name: _as_indexer(idx)})


def _date_key(text: str, end: bool) -> int:
    """'2015', '2015-03' or '2015-03-10' -> integer YYYYMMDD, padded to period start/end."""
    parts = [int(p) for p in str(text).strip().replace("/", "-").split("T")[0].split("-") if p]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Cannot parse date {text!r}; use YYYY, YYYY-MM or YYYY-MM-DD")
    pad = [None, 12, 31] if end else [None, 1, 1]
    y, m, d = parts + pad[len(parts):]
    return y * 10000 + m * 100 + d


def parse_time(time) -> tuple[int | None, int | None]:
    """Turn the ``time`` argument (None, str, slice or 2-sequence) into YYYYMMDD bounds."""
    if time is None:
        return None, None
    if isinstance(time, slice):
        start, stop = time.start, time.stop
    elif isinstance(time, (str, int)):
        start = stop = str(time)
    else:
        start, stop = time
    return (
        None if start is None else _date_key(start, end=False),
        None if stop is None else _date_key(stop, end=True),
    )


def time_keys(ds: xr.Dataset) -> np.ndarray:
    """Integer YYYYMMDD for every time step, valid for any CF calendar."""
    t = ds["time"].dt
    return (t.year.values * 10000 + t.month.values * 100 + t.day.values).astype("int64")


def time_index_range(keys: np.ndarray, start: int | None, stop: int | None) -> tuple[int, int]:
    """Half-open index range of the time steps within [start, stop]."""
    ok = np.ones(keys.shape, dtype=bool)
    if start is not None:
        ok &= keys >= start
    if stop is not None:
        ok &= keys <= stop
    hit = np.flatnonzero(ok)
    if not hit.size:
        return 0, 0
    return int(hit[0]), int(hit[-1]) + 1
