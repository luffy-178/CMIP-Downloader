# cmip6-downloader

A lightweight and fast downloader for **regional subsets of CMIP6 data** from the public
cloud mirrors (Google Cloud and AWS), built on `xarray`, `zarr` and `fsspec`.

It is the CMIP6 counterpart of [`era5-downloader`](https://pypi.org/project/era5-downloader/):
you describe the variables, models, region and period you need, and you get tidy NetCDF
files on disk. No ESGF account, no wget scripts, no downloading of global files only to
cut out one basin afterwards.

```python
from cmip6_downloader import CMIP6Downloader

dl = CMIP6Downloader(
    variables=["tos", "zos"],
    source_id=["CESM2", "MPI-ESM1-2-HR"],
    experiment_id="ssp585",
    table_id="Omon",
    bbox_WSEN=(65, 5, 80, 25),        # West, South, East, North
    region_name="wci",
)
files = dl.download(slice("2015", "2100"))
```

## Why it is fast

| What it does | Why it helps |
| --- | --- |
| Reads the Pangeo zarr copies of CMIP6 on Google Cloud (`gs://cmip6`) or AWS (`s3://cmip6-pds`) | Anonymous object storage with high throughput, instead of ESGF data nodes |
| Caches the catalogue (one CSV) locally | A search is a local file read; the catalogue is refreshed once a week |
| Subsets lazily before transferring anything | Only the variables, time chunks and levels you asked for are fetched |
| Fetches chunks and datasets in parallel | Bandwidth, not the server, becomes the limit |
| Groups output files by source chunk | A chunk holding 50 years of monthly data is fetched once, not 50 times |
| Skips files that already exist and writes atomically | An interrupted job resumes where it stopped and never leaves half-written files |

One limitation to know about: most CMIP6 zarr stores keep each time chunk as a full global
field. The bounding box therefore reduces what is held in memory and written to disk, but
the transfer saving comes mainly from the selection in time, variable and depth.

## Installation

```bash
pip install git+https://github.com/<your-username>/cmip6-downloader.git
```

or, from a clone of the repository:

```bash
pip install -e .            # add ".[aws]" to use the AWS mirror, ".[dev]" for tests
```

Python 3.10 or later is required.

## Usage

### Search

```python
from cmip6_downloader import search

hits = search(variable_id="thetao", experiment_id="historical", table_id="Omon")
hits.source_id.unique()          # which models provide it
```

`search` returns a pandas DataFrame. Every facet (`variable_id`, `source_id`,
`experiment_id`, `table_id`, `member_id`, `grid_label`, `activity_id`, `institution_id`)
accepts a string or a list of strings.

### Download

```python
from cmip6_downloader import CMIP6Downloader

dl = CMIP6Downloader(
    variables="thetao",
    source_id="CESM2",
    experiment_id="historical",
    table_id="Omon",
    member_id="r1i1p1f1",
    bbox_WSEN=(65, 5, 80, 25),
    region_name="wci",
    level_range=(0, 50000),          # CESM2 ocean depth is in centimetres
    dest_path="data/{source_id}/{variable_id}_{experiment_id}_{region_name}_{period}.nc",
    max_workers=8,
)

dl.datasets                              # the stores that matched
dl.download("2000", dry_run=True)        # list the files without downloading
files = dl.download(slice("1990", "2014"))
ds = dl.fetch_local(slice("1990", "2014"))   # open as xarray, downloading if needed
```

### Command line

```bash
cmip6-download search -v tos -e ssp585 -t Omon

cmip6-download get -v tos zos -s CESM2 -e ssp585 -t Omon \
    --bbox 65 5 80 25 --region wci --time 2015 2100 --workers 8

cmip6-download update-catalog
```

Run `cmip6-download get --help` for all options.

## Options

| Argument | Meaning |
| --- | --- |
| `variables` | CMIP6 variable id or list of ids |
| `source_id`, `experiment_id`, `table_id`, `activity_id`, `institution_id` | Facet filters (string or list) |
| `member_id` | Ensemble member(s). `None` picks one member per model run, `r1i1p1f1` when it exists. `"all"` takes every member |
| `grid_label` | `None` prefers the native grid (`gn`) over regridded output (`gr`) |
| `bbox_WSEN` | `(west, south, east, north)` in degrees. Longitudes may be given as -180..180 or 0..360, and the box may cross the date line or Greenwich |
| `region_name` | Label used in file names |
| `levels` | Vertical levels to keep, matched to the nearest model level |
| `level_range` | `(min, max)` of the vertical coordinate to keep |
| `dest_path` | Output path template. Fields: all facets, `region_name`, `period` |
| `split` | `"year"` (default), `"month"` or `"all"`: time span of each output file |
| `mirror` | `"gcs"` (default) or `"aws"` |
| `max_workers` | Number of parallel loads (default 4) |
| `max_task_mb` | Memory bound for one load, in MB of uncompressed subset (default 1024) |
| `compress` | zlib level of the NetCDF output; `0` disables compression (default 1) |
| `overwrite` | Re-download files that already exist |

The `time` argument of `download` and `fetch_local` is `None` (everything), a string
(`"2015"`, `"2015-06"`, `"2015-06-15"`) or `slice(start, end)` with both ends included.
Dates are compared as year, month and day, so they work for every CMIP6 calendar
(`noleap`, `360_day`, `proleptic_gregorian`, ...).

## Notes

- **Grids.** Regular grids, curvilinear ocean grids (2-D latitude and longitude) and
  unstructured grids are supported. On curvilinear grids the smallest index rectangle
  containing the box is returned, so a few points outside the box may be included.
- **Vertical units.** `levels` and `level_range` use the units of the dataset: Pa for
  `plev`, metres for most ocean models, centimetres for a few (for example CESM2).
- **Coverage.** The cloud mirrors hold a large part of CMIP6 but not all of it. If
  `search` returns nothing for a dataset that exists on ESGF, it has not been mirrored.
- **Catalogue cache.** Stored in `~/.cache/cmip6_downloader`; set
  `CMIP6_DOWNLOADER_CACHE` to move it (useful on HPC systems with small home quotas).
- **Tuning.** On a fast connection raise `max_workers`. If one chunk of a store is very
  large, raise `max_task_mb` so that the files sharing it are loaded together.

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The tests build small synthetic CMIP6-like zarr stores, so they run without network access.

## Acknowledgements

The cloud copies of CMIP6 are maintained by the [Pangeo](https://pangeo.io) community with
Google Cloud and AWS public datasets programmes. Please acknowledge CMIP6 and the modelling
groups according to the [CMIP6 terms of use](https://pcmdi.llnl.gov/CMIP6/TermsOfUse/).
The design follows `era5-downloader` by Luke Gregor.

## Licence

MIT
