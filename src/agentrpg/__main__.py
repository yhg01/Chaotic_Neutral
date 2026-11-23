"""Run the local board API."""

import uvicorn

from agentrpg.config import Settings
from agentrpg.main import create_app


def main() -> None:
    settings = Settings()
    # Security: local operator access trusts the real peer address. Keep proxy_headers off.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    main()
