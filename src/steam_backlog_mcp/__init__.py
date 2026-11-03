"""mcp-server-steam-backlog: an MCP server for Steam Backlog Triage."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mcp-server-steam-backlog")
except PackageNotFoundError:  # running from a checkout without pip install
    __version__ = "0.0.0"
