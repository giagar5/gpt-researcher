"""Star5 entry point: the upstream app plus the Star5 UI. Run with `uvicorn star5_main:app`."""

from main import app  # noqa: E402  (upstream entry point: logging, .env, routes)
from star5.web import install

install(app)
