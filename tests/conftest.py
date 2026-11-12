"""Synthetic CMIP6-like zarr stores so the tests need no network."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

FACETS = {
    "activity_id": "CMIP",
    "institution_id": "TEST",
    "experiment_id": "historical",
    "table_id": "Omon",
}


def _time(n_years, calendar):
    return xr.date_range("2000-01-01", periods=12 * n_years, freq="MS", calendar=calendar, use_cftime=True)


def regular_store(path, n_years=4, calendar="noleap"):
    """1-degree regular grid, longitudes 0..360, latitude stored north to south."""
    time = _time(n_years, calendar)
    lat = np.arange(89.5, -90, -1.0)
    lon = np.arange(0.5, 360, 1.0)
    data = (
        np.arange(time.size)[:, None, None] * 1000.0 + lat[None, :, None] + lon[None, None, :] / 1000.0
    ).astype("float32")
    ds = xr.Dataset(
        {"tos": (("time", "lat", "lon"), data, {"units": "degC"})},
        coords={"time": time, "lat": lat, "lon": lon},
    ).chunk({"time": 30, "lat": -1, "lon": -1})
    ds.to_zarr(path, zarr_format=2, consolidated=True)
    return ds


def curvilinear_store(path, n_years=2):
    """Ocean-style grid: 2-D latitude/longitude on (j, i), with depth levels."""
    time = _time(n_years, "360_day")
    j, i = np.arange(60), np.arange(90)
    lat2d = np.broadcast_to((-59.0 + 2 * j)[:, None], (60, 90)).copy()
    lon2d = np.broadcast_to((-178.0 + 4 * i)[None, :], (60, 90)).copy()
    lev = np.array([5.0, 15.0, 30.0, 60.0, 120.0, 250.0, 500.0, 1000.0])
    data = np.random.default_rng(0).random((time.size, lev.size, 60, 90)).astype("float32")
    ds = xr.Dataset(
        {"thetao": (("time", "lev", "j", "i"), data)},
        coords={
            "time": time,
            "lev": lev,
            "latitude": (("j", "i"), lat2d),
            "longitude": (("j", "i"), lon2d),
        },
    ).chunk({"time": 12})
    ds.to_zarr(path, zarr_format=2, consolidated=True)
    return ds


def fixed_store(path):
    lat = np.arange(-89.5, 90, 1.0)
    lon = np.arange(0.5, 360, 1.0)
    ds = xr.Dataset(
        {"deptho": (("lat", "lon"), np.ones((180, 360), "float32"))}, coords={"lat": lat, "lon": lon}
    )
    ds.to_zarr(path, zarr_format=2, consolidated=True)
    return ds


@pytest.fixture(scope="session")
def stores(tmp_path_factory):
    root = tmp_path_factory.mktemp("stores")
    rows, truth = [], {}

    def add(name, ds, **facets):
        rows.append({**FACETS, "zstore": str(root / name), "dcpp_init_year": np.nan, **facets})
        truth[name] = ds.load()

    add("reg_r1", regular_store(root / "reg_r1"), source_id="MODEL-A", member_id="r1i1p1f1",
        variable_id="tos", grid_label="gn", version=20190101)
    add("reg_r1_old", regular_store(root / "reg_r1_old"), source_id="MODEL-A", member_id="r1i1p1f1",
        variable_id="tos", grid_label="gn", version=20180101)
    add("reg_r2", regular_store(root / "reg_r2"), source_id="MODEL-A", member_id="r2i1p1f1",
        variable_id="tos", grid_label="gn", version=20190101)
    add("reg_r10", regular_store(root / "reg_r10"), source_id="MODEL-A", member_id="r10i1p1f1",
        variable_id="tos", grid_label="gr", version=20190101)
    add("curv", curvilinear_store(root / "curv"), source_id="MODEL-B", member_id="r1i1p1f1",
        variable_id="thetao", grid_label="gn", version=20200101)
    add("fx", fixed_store(root / "fx"), source_id="MODEL-A", member_id="r1i1p1f1",
        variable_id="deptho", grid_label="gn", version=20190101, table_id="Ofx")
    return pd.DataFrame(rows), truth
