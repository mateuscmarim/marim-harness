"""A tiny self-contained MCP server for trying out marim's MCP support.

Exposes three trivial tools over stdio via FastMCP. Wired up by
``.marim/mcp.json`` and launched as a subprocess when marim connects.
Safe to delete once you've finished poking at MCP.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def reverse(text: str) -> str:
    """Return TEXT reversed."""
    return text[::-1]


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@mcp.tool()
def shout(text: str) -> str:
    """Return TEXT uppercased with an exclamation mark."""
    return text.upper() + "!"


if __name__ == "__main__":
    mcp.run()
