"""Web app (FastAPI). Phase 2 adds the trades, holdings and report pages."""

from fastapi import FastAPI

from . import db
from .cli import status
from .config import load_settings

app = FastAPI(title="CSE Swing-Trading Analyzer")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status() -> dict:
    conn = db.connect(load_settings().db_path)
    try:
        return status(conn)
    finally:
        conn.close()
