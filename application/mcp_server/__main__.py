"""``python -m application.mcp_server`` entry point.

Used by the container's CMD; also handy for running the server
directly during development:

    python -m application.mcp_server
"""

from application.mcp_server.server import run


if __name__ == "__main__":
    run()
