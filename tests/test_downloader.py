import numpy as np
import pytest
import xarray as xr

import cmip6_downloader.downloader as dl_mod
from cmip6_downloader import CMIP6Downloader, search
from cmip6_downloader.catalog import member_sort_key, select_default
from cmip6_downloader.cli import main
from cmip6_downloader.downloader import _File, group_by_chunk, split_periods
from cmip6_downloader.subset import parse_time, subset_bbox, subset_levels

WCI = (65, 5, 80, 25)


def template(tmp_path):
    return str(tmp_path / "{source_id}/{variable_id}_{member_id}_{region_name}_{period}.nc")


def open_nc(path):
    return xr.open_dataset(path, decode_times=xr.coders.CFDatetimeCoder(use_cftime=True))


# ------------------------------------------------------------------ catalogue
def test_search_keeps_latest_version(stores):
    cat, _ = stores
    hits = search(cat, source_id="MODEL-A", variable_id="tos", member_id="r1i1p1f1")
    assert len(hits) == 1 and hits.version[0] == 20190101
    assert len(search(cat, source_id="MODEL-A", variable_id="tos", latest_only=False)) == 4


def test_search_rejects_unknown_facet(stores):
    with pytest.raises(TypeError):
        search(stores[0], model="MODEL-A")


def test_default_member_and_grid(stores):
    cat, _ = stores
    picked = select_default(search(cat, source_id="MODEL-A", variable_id="tos"))
    assert list(picked.member_id) == ["r1i1p1f1"]
    assert sorted(["r10i1p1f1", "r2i1p1f1", "r1i1p1f1"], key=member_sort_key)[1] == "r2i1p1f1"


def test_no_match_raises(stores):
    with pytest.raises(LookupError):
        CMIP6Downloader("zos", catalog=stores[0])


# ------------------------------------------------------------------ subsetting
def test_bbox_regular_grid(stores):
    truth = stores[1]["reg_r1"]
    sub = subset_bbox(truth, WCI)
    assert sub.lon.min() == 65.5 and sub.lon.max() == 79.5
    assert sub.lat.min() == 5.5 and sub.lat.max() == 24.5


def test_bbox_across_seam_is_contiguous(stores):
    sub = subset_bbox(stores[1]["reg_r1"], (-20, 0, 15, 10))
    lon = sub.lon.values
    assert lon[0] == 340.5 and lon[-1] == 14.5 and lon.size == 35


def test_bbox_curvilinear(stores):
    truth = stores[1]["curv"]
    sub = subset_bbox(truth, WCI)
    assert 0 < sub.sizes["i"] < truth.sizes["i"] and 0 < sub.sizes["j"] < truth.sizes["j"]
    assert float(sub.longitude.min()) >= 65 and float(sub.longitude.max()) <= 80
    assert float(sub.latitude.min()) >= 5 and float(sub.latitude.max()) <= 25


def test_bbox_outside_grid_raises(stores):
    with pytest.raises(ValueError):
        subset_bbox(stores[1]["curv"].isel(j=slice(0, 5)), (65, 80, 80, 89))


def test_levels(stores):
    truth = stores[1]["curv"]
    assert list(subset_levels(truth, "thetao", level_range=(0, 200)).lev) == [5, 15, 30, 60, 120]
    assert list(subset_levels(truth, "thetao", levels=[10, 100])["lev"]) == [5, 120]
    assert subset_levels(stores[1]["reg_r1"], "tos", levels=[10]).sizes == stores[1]["reg_r1"].sizes


def test_parse_time():
    assert parse_time("2015") == (20150101, 20151231)
    assert parse_time(slice("2015-03", "2016-02")) == (20150301, 20160231)
    assert parse_time(None) == (None, None)


# -------------------------------------------------------------------- planning
def test_split_periods():
    keys = np.array([20000101 + m * 100 for m in range(12)] + [20010101 + m * 100 for m in range(12)])
    assert split_periods(keys, 6, 18, "year") == [("2000", 6, 12), ("2001", 12, 18)]
    assert len(split_periods(keys, 0, 24, "month")) == 24
    assert split_periods(keys, 0, 24, "all") == [("200001-200112", 0, 24)]


def test_group_by_chunk_merges_files_sharing_a_chunk():
    files = [_File(None, a, a + 12) for a in range(0, 48, 12)]
    edges = np.cumsum([30, 18])  # year 3 straddles both chunks, chaining all four
    groups = group_by_chunk(files, edges, steps_per_task=10_000)
    assert [len(g) for g in groups] == [4]
    assert [len(g) for g in group_by_chunk(files, edges, steps_per_task=24)] == [2, 2]
    assert [len(g) for g in group_by_chunk(files, np.cumsum([12] * 4), 10_000)] == [1, 1, 1, 1]


# ------------------------------------------------------------------- downloads
def test_download_values_match_source(stores, tmp_path):
    cat, truth = stores
    d = CMIP6Downloader("tos", source_id="MODEL-A", catalog=cat, bbox_WSEN=WCI,
                        region_name="wci", dest_path=template(tmp_path))
    paths = d.download(slice("2001", "2002"))
    assert [p.name for p in paths] == ["tos_r1i1p1f1_wci_2001.nc", "tos_r1i1p1f1_wci_2002.nc"]
    got = xr.concat([open_nc(p) for p in paths], "time")
    want = subset_bbox(truth["reg_r1"], WCI).isel(time=slice(12, 36))
    np.testing.assert_array_equal(got.tos.values, want.tos.values)
    assert got.time.values[0] == want.time.values[0]
    assert got.time.dt.calendar == "noleap"
    assert not list(tmp_path.rglob("*.part"))


def test_download_resumes_and_loads_shared_chunk_once(stores, tmp_path, monkeypatch):
    cat, _ = stores
    kw = {"source_id": "MODEL-A", "catalog": cat, "bbox_WSEN": WCI, "dest_path": template(tmp_path)}
    calls = []
    real = dl_mod.CMIP6Downloader._run
    monkeypatch.setattr(dl_mod.CMIP6Downloader, "_run", lambda s, t: calls.append(t) or real(s, t))

    paths = CMIP6Downloader("tos", **kw).download()
    assert len(paths) == 4 and all(p.exists() for p in paths)
    assert len(calls) < 4  # yearly files sharing 30-step chunks were loaded together

    calls.clear()
    assert CMIP6Downloader("tos", **kw).download() == paths
    assert calls == []  # nothing left to do

    paths[2].unlink()
    CMIP6Downloader("tos", **kw).download()
    assert paths[2].exists() and sum(len(t.files) for t in calls) == 1


def test_download_curvilinear_levels_month_split(stores, tmp_path):
    cat, truth = stores
    d = CMIP6Downloader("thetao", source_id="MODEL-B", catalog=cat, bbox_WSEN=WCI,
                        level_range=(0, 200), split="month", dest_path=template(tmp_path))
    paths = d.download("2000-02")
    assert [p.name for p in paths] == ["thetao_r1i1p1f1_region_200002.nc"]
    got = open_nc(paths[0])
    want = subset_levels(subset_bbox(truth["curv"], WCI), "thetao", level_range=(0, 200)).isel(time=[1])
    np.testing.assert_array_equal(got.thetao.values, want.thetao.values)
    assert got.sizes["lev"] == 5


def test_fixed_field(stores, tmp_path):
    d = CMIP6Downloader("deptho", table_id="Ofx", catalog=stores[0], dest_path=template(tmp_path))
    (path,) = d.download(slice("2000", "2001"))
    assert path.name == "deptho_r1i1p1f1_global_fx.nc" and open_nc(path).deptho.shape == (180, 360)


def test_all_members_and_fetch_local(stores, tmp_path):
    cat, _ = stores
    d = CMIP6Downloader("tos", source_id="MODEL-A", member_id="all", grid_label="gn", catalog=cat,
                        bbox_WSEN=WCI, dest_path=template(tmp_path))
    out = d.fetch_local("2000")
    assert sorted(k.split(".")[2] for k in out) == ["r1i1p1f1", "r2i1p1f1"]
    single = CMIP6Downloader("tos", source_id="MODEL-A", catalog=cat, bbox_WSEN=WCI,
                             dest_path=template(tmp_path)).fetch_local(slice("2000", "2001"))
    assert single.sizes["time"] == 24


def test_dry_run_writes_nothing(stores, tmp_path):
    d = CMIP6Downloader("tos", source_id="MODEL-A", catalog=stores[0], dest_path=template(tmp_path))
    paths = d.download("2000", dry_run=True)
    assert len(paths) == 1 and not paths[0].exists()


def test_cli_parses():
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
