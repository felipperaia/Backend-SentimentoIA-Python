"""Compatibility ASGI entrypoint.

This module exists for legacy deploy commands that still reference
`app.main_real:app`. It proxies directly to the canonical app in `app.main`.
"""

from app.main import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
