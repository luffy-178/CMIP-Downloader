"""Fast, subsetting downloader for CMIP3, CMIP5, CMIP6 and CMIP7 data."""

from .catalog import load_catalog, search
from .downloader import CMIP6Downloader, CMIPDownloader

__version__ = "0.2.1"
__all__ = ["CMIP6Downloader", "CMIPDownloader", "__version__", "load_catalog", "search"]
