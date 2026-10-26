"""Local broadband API. Swagger and schema require DOCS_USERNAME / DOCS_PASSWORD."""

import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from database import (
    DB_PATH,
    create_tables,
    get_latest_address_check,
    save_address_check,
)
from inspect_verizon import inspect as inspect_verizon

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
basic = HTTPBasic()


def require_docs(credentials: HTTPBasicCredentials = Depends(basic)):
    username, password = os.getenv("DOCS_USERNAME"), os.getenv("DOCS_PASSWORD")
    if not username or not password:
        raise HTTPException(503, "Configure DOCS_USERNAME and DOCS_PASSWORD in .env")
    user_ok = secrets.compare_digest(credentials.username.encode(), username.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            401,
            "Incorrect username or password",
            headers={"WWW-Authenticate": 'Basic realm="Swagger"'},
        )
    return credentials.username


@asynccontextmanager
async def lifespan(app):
    create_tables()
    yield


app = FastAPI(
    title="Broadband Comparison API",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


class VerizonAddressRequest(BaseModel):
    address: str = Field(min_length=5, max_length=300)
    zip_code: str | None = Field(default=None, min_length=5, max_length=10)
    wait_seconds: int = Field(default=90, ge=10, le=600)


@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_docs)])
def docs():
    return get_swagger_ui_html(
        openapi_url="/openapi.json", title="Broadband API — Swagger"
    )


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_docs)])
def schema():
    return app.openapi()


def connect():
    path = Path(DB_PATH).resolve()
    if not path.exists():
        raise HTTPException(503, "Database is missing. Run the scrapers.")
    c = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def read_plans(table, provider=None):
    if table not in {"plans", "provider_plans"}:
        raise ValueError("Unknown table")
    c = connect()
    try:
        if not c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            return {"total": 0, "plans": []}
        sql = f'SELECT * FROM "{table}"'
        values = []
        if provider:
            sql += " WHERE provider = ? COLLATE NOCASE"
            values.append(provider.strip())
        sql += " ORDER BY provider, id"
        rows = [dict(r) for r in c.execute(sql, values)]
        if table == "provider_plans":
            for row in rows:
                row["conditions"] = json.loads(row.pop("conditions_json"))
        return {"total": len(rows), "plans": rows}
    finally:
        c.close()


@app.get("/plans", tags=["Legacy Jio data"])
def plans():
    return read_plans("plans")


@app.get("/provider-plans", tags=["All provider data"])
def provider_plans(provider: str | None = None):
    return read_plans("provider_plans", provider)


@app.get("/plans/jio", tags=["Jio"])
def jio():
    return read_plans("plans", "JioFiber")


@app.get("/plans/hathway", tags=["Hathway"])
def hathway():
    return read_plans("provider_plans", "Hathway")


@app.get("/plans/act", tags=["ACT"])
def act():
    return read_plans("provider_plans", "ACT")


@app.post("/verizon/check", tags=["Verizon residential home internet"])
def check_verizon_address(request: VerizonAddressRequest):
    """Check one address, save the result and any candidate plans in SQLite."""
    if request.zip_code and not re.fullmatch(
        r"\d{5}(?:-\d{4})?", request.zip_code.strip()
    ):
        raise HTTPException(
            422, "ZIP code must have 5 digits, optionally followed by 4 digits."
        )
    try:
        result = inspect_verizon(
            request.address.strip(),
            request.zip_code.strip() if request.zip_code else None,
            request.wait_seconds,
            False,
        )
        check_id = save_address_check(result)
        result["check_id"] = check_id
        return result
    except ImportError as error:
        raise HTTPException(503, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except Exception as error:
        raise HTTPException(502, f"Verizon lookup failed: {error}") from error


@app.get("/verizon/results", tags=["Verizon residential home internet"])
def verizon_results(address: str = Query(min_length=5, max_length=300)):
    """Read the latest saved Verizon lookup for an exact address."""
    result = get_latest_address_check(address)
    if result is None:
        return {
            "found": False,
            "address": address,
            "message": "No saved Verizon lookup for this address.",
        }
    return {"found": True, **result}


@app.get("/database-status", tags=["Database"])
def database_status():
    c = connect()
    try:
        tables = []
        for name in ("plans", "provider_plans", "address_checks"):
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone():
                time_column = "checked_at" if name == "address_checks" else "scraped_at"
                rows = [
                    dict(r)
                    for r in c.execute(
                        f'SELECT provider, COUNT(*) AS count, MAX("{time_column}") AS last_scraped FROM "{name}" GROUP BY provider'
                    )
                ]
                tables.append(
                    {
                        "name": name,
                        "providers": rows,
                        "count": sum(r["count"] for r in rows),
                    }
                )
        return {
            "exists": True,
            "database_path": str(Path(DB_PATH).resolve()),
            "size_bytes": Path(DB_PATH).stat().st_size,
            "tables": tables,
            "total": sum(t["count"] for t in tables),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        c.close()


@app.get("/", include_in_schema=False)
def dashboard():
    frontend = BASE_DIR / "frontend" / "index.html"
    if not frontend.exists():
        frontend = BASE_DIR / "index.html"
    if not frontend.exists():
        raise HTTPException(404, "Frontend index.html was not found.")
    return FileResponse(frontend)
