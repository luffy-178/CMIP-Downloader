"""Download CMIP5 SST for the west coast of India from ESGF.

Needs the ESGF extra: pip install "cmip-downloader[esgf]"
"""

import logging

from cmip_downloader import CMIPDownloader

logging.basicConfig(level=logging.INFO, format="%(message)s")

dl = CMIPDownloader(
    variables="tos",
    mip_era="CMIP5",
    source_id=["CCSM4", "MPI-ESM-MR"],
    experiment_id="rcp85",
    table_id="Omon",
    bbox_WSEN=(65, 5, 80, 25),
    region_name="wci",
    keep_raw=False,  # delete the global source files once the subsets are written
)
print(dl.datasets[["mip_era", "source_id", "experiment_id", "member_id", "variable_id"]])
files = dl.download(slice("2006", "2100"))
print(f"{len(files)} files ready")
