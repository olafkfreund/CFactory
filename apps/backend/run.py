"""Local entry point: run the CFactory backend with uvicorn.

    python apps/backend/run.py
"""

from __future__ import annotations

import uvicorn

from cfactory.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "cfactory.app:app",
        host="0.0.0.0",
        port=settings.backend_port,
        reload=True,
        # The httptools HTTP impl does not forward the WebSocket Upgrade in this
        # environment; h11 + wsproto handles the /api/ws cockpit feed correctly.
        http="h11",
        ws="wsproto",
    )


if __name__ == "__main__":
    main()
