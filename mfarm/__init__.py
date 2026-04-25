from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("mfarm")
except PackageNotFoundError:
    __version__ = "unknown"
