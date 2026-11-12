"""Download SST and sea level for the west coast of India from two models."""

import logging

from cmip_downloader import CMIPDownloader

logging.basicConfig(level=logging.INFO, format="%(message)s")

dl = CMIPDownloader(
    variables=["tos", "zos"],
    source_id=["CESM2", "MPI-ESM1-2-HR"],
    experiment_id=["historical", "ssp585"],
    table_id="Omon",
    bbox_WSEN=(65, 5, 80, 25),
    region_name="wci",
    max_workers=8,
)
print(dl.datasets[["source_id", "experiment_id", "member_id", "variable_id", "grid_label"]])
files = dl.download(slice("1990", "2100"))
print(f"{len(files)} files ready")
