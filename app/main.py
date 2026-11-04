"""FastAPI application entrypoint: ``uvicorn app.main:app``."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.routes import router
from app.errors import DomainError

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Inventory & Warehouse Management", version="1.0.0")
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError):
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


@app.exception_handler(IntegrityError)
async def integrity_error_handler(_: Request, exc: IntegrityError):
    # Last line of defence: a DB constraint (negative stock, capacity, uniqueness) rejected the write.
    return JSONResponse(status_code=409, content={"error": {
        "code": "integrity_error", "message": "Operation violates a data integrity constraint", "details": {}}})


@app.exception_handler(OperationalError)
async def operational_error_handler(_: Request, exc: OperationalError):
    return JSONResponse(status_code=503, content={"error": {
        "code": "database_busy", "message": "Database temporarily unavailable; retry", "details": {}}})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
