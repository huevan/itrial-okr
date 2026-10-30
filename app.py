"""Standalone OKR composition root; does not import any other DTP module."""
from contextlib import asynccontextmanager
import json

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from okr.paths import CONFIG_PATH, STATIC_DIR
from okr.public import init_db, okr_router


@asynccontextmanager
async def lifespan(app):
    if not CONFIG_PATH.is_file():
        raise RuntimeError("Run python scripts/setup.py before starting OKR")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(config.get("jwt_secret"), str) or len(config["jwt_secret"]) < 32:
        raise RuntimeError("config.json must contain a random jwt_secret of at least 32 characters")
    init_db()
    yield


app = FastAPI(title="iTrial OKR", version="1.0.0", lifespan=lifespan)
app.include_router(okr_router)
app.mount("/static/okr", StaticFiles(directory=str(STATIC_DIR)), name="okr-static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/okr")


@app.get("/healthz", include_in_schema=False)
async def health():
    return {"status": "ok"}
