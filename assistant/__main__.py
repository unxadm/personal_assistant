"""Run the server: ``python -m assistant`` or the ``assistant`` console script."""

from __future__ import annotations

import uvicorn

from .api.app import create_app
from .core.config import ConfigService


def main() -> None:
    config = ConfigService.load()
    server = config.config.server
    uvicorn.run(create_app(config), host=server.host, port=server.port, log_config=None)


if __name__ == "__main__":
    main()
