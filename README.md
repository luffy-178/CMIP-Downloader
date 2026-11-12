# cmip-downloader

A lightweight and fast downloader for **regional subsets of CMIP data** — CMIP3, CMIP5,
CMIP6 and CMIP7 — built on `xarray`, `zarr`, `fsspec` and `intake-esgf`.

You describe the variables, models, region and period you need, and you get tidy NetCDF
files on disk. No wget scripts, and no hand-cutting of global files to one basin.

```python
from cmip_downloader import CMIPDownloader

dl = CMIPDownloader(
    variables=["tos", "zos"],
    source_id=["CESM2", "MPI-ESM1-2-HR"],
    experiment_id="ssp585",
    table_id="Omon",
    bbox_WSEN=(65, 5, 80, 25),        # West, South, East, North
    region_name="wci",
)
files = dl.download(slice("2015", "2100"))
```

The same call works for the other phases by adding `mip_era`:

```python
dl = CMIPDownloader("tos", mip_era="CMIP5", source_id="CCSM4",
                    experiment_id="rcp85", table_id="Omon", bbox_WSEN=(65, 5, 80, 25))
```

## Where the data comes from

| Phase | Source | How it is fetched |
| --- | --- | --- |
| CMIP6 | Pangeo zarr copies on Google Cloud (`gs://cmip6`) or AWS (`s3://cmip6-pds`) | Only the requested variables, time chunks and levels are transferred |
| CMIP6 not on the cloud | ESGF (automatic fall-back) | Whole NetCDF files overlapping the period, then cut locally |
| CMIP7, CMIP5, CMIP3 | ESGF, through [`intake-esgf`](https://intake-esgf.readthedocs.io) | Whole NetCDF files overlapping the period, then cut locally |

The cloud mirrors hold CMIP6 only, so that is the fast path. ESGF serves complete global
files: the download of those is parallel, checksum-verified and cached, but its volume is
set by the files, not by your region. The output is the same in both cases.

## Why it is fast

| What it does | Why it helps |
| --- | --- |
| Reads CMIP6 from anonymous cloud object storage | High throughput, no ESGF data-node queues |
| Caches the cloud catalogue (one CSV) locally | A search is a local file read, refreshed once a week |
| Subsets lazily before transferring anything (cloud) | Only what you asked for is fetched |
| Fetches chunks, files and datasets in parallel | Bandwidth, not the server, becomes the limit |
| Groups output files by source chunk | A chunk holding 50 years of monthly data is fetched once, not 50 times |
| Downloads only the ESGF files overlapping the period | No transfer of decades you did not ask for |
| Skips files that already exist and writes atomically | An interrupted job resumes and never leaves half-written files |

One limitation on the cloud path: most CMIP6 zarr stores keep each time chunk as a full
global field. The bounding box therefore reduces memory and disk use, while the transfer
saving comes mainly from the selection in time, variable and depth.

## Installation

```bash
pip install "cmip-downloader[esgf]"
```

Leave out `[esgf]` if you only need CMIP6 from the cloud. For the development version:

```bash
pip install "cmip-downloader[esgf] @ git+https://github.com/luffy-178/CMIP-Downloader.git"
```

From a clone of the repository:

```bash
pip install -e ".[esgf]"     # extras: esgf, aws, all, dev
```

Python 3.10 or later is required.

## Usage

### Search

```python
from cmip_downloader import search

search(variable_id="thetao", experiment_id="historical", table_id="Omon")   # CMIP6, cloud
search(mip_era="CMIP5", variable_id="tos", experiment_id="rcp85")           # ESGF
search(mip_era="CMIP7", variable_id="tos", frequency="mon")                 # ESGF
```

`search` returns a pandas DataFrame with the same columns for every phase.

### Download

```python
from cmip_downloader import CMIPDownloader

dl = CMIPDownloader(
    variables="thetao",
    source_id="CESM2",
    experiment_id="historical",
    table_id="Omon",
    member_id="r1i1p1f1",
    bbox_WSEN=(65, 5, 80, 25),
    region_name="wci",
    level_range=(0, 50000),          # CESM2 ocean depth is in centimetres
    dest_path="data/{mip_era}/{source_id}/{variable_id}_{experiment_id}_{region_name}_{period}.nc",
    max_workers=8,
)

dl.datasets                              # the datasets that matched
dl.source                                # "cloud" or "esgf"
dl.download("2000", dry_run=True)        # list the files without downloading (cloud only)
files = dl.download(slice("1990", "2014"))
ds = dl.fetch_local(slice("1990", "2014"))   # open as xarray, downloading if needed
```

### Command line

```bash
cmip-download search -v tos -e ssp585 -t Omon
cmip-download search -v tos -e rcp85 --mip-era CMIP5

cmip-download get -v tos zos -s CESM2 -e ssp585 -t Omon \
    --bbox 65 5 80 25 --region wci --time 2015 2100 --workers 8

cmip-download get -v tos -s CCSM4 -e rcp85 -t Omon --mip-era CMIP5 \
    --bbox 65 5 80 25 --region wci --time 2006 2100 --discard-raw

cmip-download update-catalog
```

Run `cmip-download get --help` for all options.

## Facet names across phases

You always use the CMIP6 names. They are translated as follows:

| You write | CMIP6 | CMIP7 | CMIP5 | CMIP3 |
| --- | --- | --- | --- | --- |
| `variables` | `variable_id` | `variable_id` | `variable` | `variable` |
| `source_id` | `source_id` | `source_id` | `model` | `model` |
| `experiment_id` | `experiment_id` | `experiment_id` | `experiment` | `experiment` |
| `member_id` | `member_id` | `variant_label` | `ensemble` | `ensemble` |
| `table_id` | `table_id` | not available | `cmor_table` | not available |
| `frequency` | use `table_id` | `frequency` | `time_frequency` | `time_frequency` |
| `grid_label` | `grid_label` | `grid_label` | not available | not available |
| `institution_id` | `institution_id` | `institution_id` | `institute` | `institute` |

Anything else goes through `extra_facets` in the phase's own vocabulary, for example
`extra_facets={"realm": "ocean"}` for CMIP3, or
`extra_facets={"variable_branding_suffix": "tavg-u-hxy-sea"}` for CMIP7. In file names,
facets a phase does not have are written as `na`; for CMIP7 and CMIP3 the `table_id`
field holds the frequency (with branding suffix and region for CMIP7).

## Options

| Argument | Meaning |
| --- | --- |
| `variables` | Variable id or list of ids |
| `mip_era` | `"CMIP6"` (default), `"CMIP7"`, `"CMIP5"` or `"CMIP3"` |
| `source` | `"auto"` (default), `"cloud"` or `"esgf"` |
| `source_id`, `experiment_id`, `table_id`, `activity_id`, `institution_id`, `frequency` | Facet filters (string or list) |
| `member_id` | Ensemble member(s). `None` picks one member per model run, the first realisation when it exists. `"all"` takes every member |
| `grid_label` | `None` prefers the native grid (`gn`) over regridded output (`gr`) |
| `extra_facets` | Dictionary of further ESGF search terms |
| `bbox_WSEN` | `(west, south, east, north)` in degrees. Longitudes may be given as -180..180 or 0..360, and the box may cross the date line or Greenwich |
| `region_name` | Label used in file names |
| `levels` | Vertical levels to keep, matched to the nearest model level |
| `level_range` | `(min, max)` of the vertical coordinate to keep |
| `dest_path` | Output path template. Fields: `mip_era`, all facets, `region_name`, `period` |
| `split` | `"year"` (default), `"month"` or `"all"`: time span of each output file |
| `mirror` | Cloud mirror: `"gcs"` (default) or `"aws"` |
| `max_workers` | Number of parallel loads and downloads (default 4) |
| `max_task_mb` | Memory bound for one load, in MB of uncompressed subset (default 1024) |
| `compress` | zlib level of the NetCDF output; `0` disables compression (default 1) |
| `raw_cache` | Directory for global files downloaded from ESGF (default `~/.esgf`) |
| `keep_raw` | Keep those files after subsetting (default `True`) |
| `overwrite` | Re-download files that already exist |

The `time` argument of `download` and `fetch_local` is `None` (everything), a string
(`"2015"`, `"2015-06"`, `"2015-06-15"`) or `slice(start, end)` with both ends included.
Dates are compared as year, month and day, so they work for every CMIP calendar
(`noleap`, `360_day`, `proleptic_gregorian`, ...).

## Notes

- **ESGF disk use.** Global source files are kept in `raw_cache` so that a second region
  needs no new download. They can be large for 3-D ocean fields; set `keep_raw=False`
  (`--discard-raw`) to delete them once the subsets are written.
- **ESGF is changing.** ESGF moved to new infrastructure in 2026 and older phases are
  being migrated. Searching is delegated to `intake-esgf`, the client maintained by ESGF,
  so keep it up to date (`pip install -U intake-esgf`) if a phase stops returning results.
- **CMIP7 is still being published.** Expect few models at first.
- **Grids.** Regular grids, curvilinear ocean grids (2-D latitude and longitude) and
  unstructured grids are supported. On curvilinear grids the smallest index rectangle
  containing the box is returned, so a few points outside the box may be included.
- **Vertical units.** `levels` and `level_range` use the units of the dataset: Pa for
  `plev`, metres for most ocean models, centimetres for a few (for example CESM2).
- **Catalogue cache.** Stored in `~/.cache/cmip_downloader`; set `CMIP_DOWNLOADER_CACHE`
  to move it (useful on HPC systems with small home quotas).
- **Tuning.** On a fast connection raise `max_workers`. If one chunk of a store is very
  large, raise `max_task_mb` so that the files sharing it are loaded together.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The tests build small synthetic CMIP-like stores and files, so they run without network
access.

## Acknowledgements

The cloud copies of CMIP6 are maintained by the [Pangeo](https://pangeo.io) community with
the Google Cloud and AWS public datasets programmes. ESGF access relies on
[`intake-esgf`](https://github.com/esgf2-us/intake-esgf). Please acknowledge CMIP and the
modelling groups according to the terms of use of each phase
([CMIP6 terms](https://pcmdi.llnl.gov/CMIP6/TermsOfUse/)). The design follows
`era5-downloader` by Luke Gregor.

## Licence

MIT
