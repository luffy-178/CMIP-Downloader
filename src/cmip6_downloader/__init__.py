"""Fast, subsetting downloader for CMIP6 data from the public cloud mirrors."""

from .catalog import load_catalog, search
from .downloader import CMIP6Downloader

__version__ = "0.1.0"
__all__ = ["CMIP6Downloader", "__version__", "load_catalog", "search"]
