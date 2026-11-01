"""Run the local board API."""

import uvicorn

from agentrpg.config import Settings
from agentrpg.main import create_app


def main() -> None:
    settings = Settings()
    # Local operator access uses the actual peer address, never forwarded data.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, proxy_headers=False)


if __name__ == "__main__":
    main()
