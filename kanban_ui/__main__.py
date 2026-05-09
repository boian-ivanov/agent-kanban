"""Entry point: ``python -m kanban_ui`` — runs uvicorn on 127.0.0.1:7777."""
import os

import uvicorn


def main() -> None:
    host = os.environ.get("KANBAN_HOST", "127.0.0.1")
    port = int(os.environ.get("KANBAN_PORT", "7777"))
    uvicorn.run(
        "kanban_ui.main:app",
        host=host,
        port=port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
