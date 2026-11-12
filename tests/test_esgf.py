"""ESGF back end, tested against a stand-in for the intake-esgf catalogue."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from cmip_downloader import CMIPDownloader
from cmip_downloader.esgf import normalise, translate_facets
from cmip_downloader.subset import subset_bbox

WCI = (65, 5, 80, 25)
CMIP5_MASTER = ["institute", "model", "experiment", "time_frequency", "realm", "cmor_table", "ensemble",
                "variable"]


class FakeProject:
    def __init__(self, master):
        self._master = master

    def master_id_facets(self):
        return self._master


class FakeCatalog:
    """Mimics the parts of intake_esgf.ESGFCatalog that the back end relies on."""

    def __init__(self, df, files, master):
        self._all, self._files = df, files
        self.project = FakeProject(master)
        self.df = self.file_start = self.file_end = self.terms = None

    def search(self, quiet=False, **terms):
        self.terms = terms
        mask = pd.Series(True, index=self._all.index)
        for key, value in terms.items():
            if key in self._all.columns:
                mask &= self._all[key].isin([value] if isinstance(value, str) else list(value))
        self.df = self._all[mask].copy()
        if self.df.empty:
            raise NoSearchResults()
        return self

    def to_path_dict(self, minimal_keys=True, separator=".", quiet=False):
        master = self.project.master_id_facets()
        self.df["key"] = self.df[master].astype(str).agg(separator.join, axis=1)
        out = {}
        for idx, key in self.df["key"].items():
            keep = [
                path for path, first, last in self._files[idx]
                if not (self.file_start is not None and last < self.file_start)
                and not (self.file_end is not None and first > self.file_end)
            ]
            if keep:
                out[key] = keep
        return out


class NoSearchResults(Exception):
    pass


def write_files(root, name, var="tos", years=((2000, 2001), (2002, 2003))):
    """Two consecutive global NetCDF files, as ESGF would serve them."""
    lat, lon = np.arange(-89.5, 90, 1.0), np.arange(0.5, 360, 1.0)
    files, parts = [], []
    for y0, y1 in years:
        time = xr.date_range(f"{y0}-01-01", periods=12 * (y1 - y0 + 1), freq="MS", calendar="noleap",
                             use_cftime=True)
        data = (y0 + np.arange(time.size)[:, None, None] + lat[None, :, None] / 100
                + lon[None, None, :] / 1e5).astype("float32")
        ds = xr.Dataset({var: (("time", "lat", "lon"), data)}, coords={"time": time, "lat": lat, "lon": lon})
        path = root / name / f"{var}_{y0}01-{y1}12.nc"
        path.parent.mkdir(parents=True, exist_ok=True)
        ds.to_netcdf(path)
        files.append((path, pd.Timestamp(f"{y0}-01-01"), pd.Timestamp(f"{y1}-12-31")))
        parts.append(ds)
    return files, xr.concat(parts, "time")


@pytest.fixture
def cmip5(tmp_path):
    rows, files, truth = [], {}, {}
    for i, ens in enumerate(["r2i1p1", "r1i1p1"]):
        rows.append({"project": "CMIP5", "institute": "INST", "model": "MODEL-5", "experiment": "rcp85",
                     "time_frequency": "mon", "realm": "ocean", "cmor_table": "Omon", "ensemble": ens,
                     "variable": "tos", "version": "20120101"})
        files[i], truth[ens] = write_files(tmp_path / "raw", ens)
    return FakeCatalog(pd.DataFrame(rows), files, CMIP5_MASTER), truth


def template(tmp_path):
    return str(tmp_path / "out/{mip_era}/{source_id}_{variable_id}_{member_id}_{table_id}_{period}.nc")


def test_translate_facets():
    assert translate_facets("cmip5", variable_id="tos", source_id="CCSM4", member_id=None) == {
        "project": "CMIP5", "variable": "tos", "model": "CCSM4"}
    assert translate_facets("CMIP7", member_id="r1i1p1f1", frequency="mon",
                            extra_facets={"region": "glb"}) == {
        "project": "CMIP7", "variant_label": "r1i1p1f1", "frequency": "mon", "region": "glb"}
    with pytest.raises(ValueError, match="no MIP tables"):
        translate_facets("CMIP7", table_id="Omon")
    with pytest.raises(ValueError, match="mip_era"):
        translate_facets("CMIP8")


def test_normalise_cmip7_keeps_datasets_apart():
    df = pd.DataFrame({
        "source_id": ["M7", "M7"], "experiment_id": ["historical"] * 2, "variant_label": ["r1i1p1f1"] * 2,
        "frequency": ["mon"] * 2, "variable_id": ["tos"] * 2, "grid_label": ["gn"] * 2,
        "variable_branding_suffix": ["tavg-u-hxy-sea", "tpt-u-hxy-sea"], "region": ["glb"] * 2,
        "activity_id": ["CMIP"] * 2, "institution_id": ["I"] * 2, "version": ["v20260801"] * 2})
    out = normalise("CMIP7", df)
    assert list(out.table_id) == ["mon_tavg-u-hxy-sea_glb", "mon_tpt-u-hxy-sea_glb"]
    assert list(out.member_id) == ["r1i1p1f1"] * 2 and out.version[0] == 20260801


def test_cmip5_download_subsets_esgf_files(cmip5, tmp_path):
    fake, truth = cmip5
    d = CMIPDownloader("tos", mip_era="CMIP5", source_id="MODEL-5", experiment_id="rcp85", table_id="Omon",
                       bbox_WSEN=WCI, dest_path=template(tmp_path), esgf_catalog=fake)
    assert d.source == "esgf" and list(d.datasets.member_id) == ["r1i1p1"]  # first member by default
    assert fake.terms == {"project": "CMIP5", "variable": "tos", "model": "MODEL-5", "experiment": "rcp85",
                          "cmor_table": "Omon"}
    paths = d.download(slice("2001", "2002"))
    assert [p.name for p in paths] == ["MODEL-5_tos_r1i1p1_Omon_2001.nc", "MODEL-5_tos_r1i1p1_Omon_2002.nc"]
    assert paths[0].parent.name == "CMIP5"
    coder = xr.coders.CFDatetimeCoder(use_cftime=True)
    got = xr.concat([xr.open_dataset(p, decode_times=coder) for p in paths], "time")
    want = subset_bbox(truth["r1i1p1"], WCI).isel(time=slice(12, 36))
    np.testing.assert_array_equal(got.tos.values, want.tos.values)
    assert (tmp_path / "raw/r1i1p1").exists()  # source files kept by default


def test_only_files_in_period_are_fetched_and_raw_can_be_discarded(cmip5, tmp_path):
    fake, _ = cmip5
    d = CMIPDownloader("tos", mip_era="CMIP5", member_id="all", dest_path=template(tmp_path),
                       esgf_catalog=fake, keep_raw=False)
    assert len(d.datasets) == 2
    assert d.download("2003", dry_run=True) == []
    out = d.fetch_local("2003")
    assert len(out) == 2 and all(ds.sizes["time"] == 12 for ds in out.values())
    left = sorted(p.name for p in (tmp_path / "raw").rglob("*.nc"))
    assert left == ["tos_200001-200112.nc"] * 2  # 2002-2003 files used then removed, others never touched


def test_phase_and_source_validation(cmip5):
    with pytest.raises(ValueError, match="CMIP6 only"):
        CMIPDownloader("tos", mip_era="CMIP5", source="cloud")
    with pytest.raises(LookupError):
        CMIPDownloader("zos", mip_era="CMIP5", esgf_catalog=cmip5[0])
