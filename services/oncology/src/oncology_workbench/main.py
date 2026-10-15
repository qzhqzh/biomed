import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from .engine import extract_fields, search_evidence
from .models import Evidence, ExtractRequest, IssueCreate, IssueUpdate, SearchRequest
from .store import IssueStore

ROOT = Path(__file__).resolve().parents[2]


def create_app(data_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.catalog = TypeAdapter(list[Evidence]).validate_json(
            (ROOT / "data/demo/evidence.json").read_text()
        )
        ids = [item.id for item in app.state.catalog]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate evidence identifiers")
        app.state.store = IssueStore(
            (data_dir or Path(os.getenv("ONCO_DATA_DIR", str(ROOT / "var")))) / "lessons.sqlite3"
        )
        yield

    app = FastAPI(title="肿瘤工作台 API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def demo_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "仅允许同源请求"}, status_code=403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 32768:
                return JSONResponse({"detail": "请求内容过大"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path == "/":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'"
            )
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": "synthetic-demo", "version": "0.1.0"}

    @app.get("/api/catalog")
    def catalog():
        return {"items": app.state.catalog, "mode": "synthetic-demo"}

    @app.get("/api/cases")
    def cases():
        return {
            "items": json.loads((ROOT / "data/demo/cases.json").read_text()),
            "mode": "synthetic-demo",
        }

    @app.post("/api/extract")
    def extract(payload: ExtractRequest):
        return extract_fields(payload.text)

    @app.post("/api/search")
    def search(payload: SearchRequest):
        return search_evidence(payload.fields, app.state.catalog)

    @app.get("/api/issues")
    def issues():
        return {"items": app.state.store.list()}

    @app.post("/api/issues", status_code=201)
    def add_issue(payload: IssueCreate):
        return app.state.store.create(payload)

    @app.patch("/api/issues/{issue_id}")
    def update_issue(issue_id: UUID, payload: IssueUpdate):
        item = app.state.store.update(str(issue_id), payload)
        if item is None:
            raise HTTPException(status_code=404, detail="问题记录不存在")
        return item

    @app.get("/api/issues/export")
    def export_issues():
        return JSONResponse(
            content={"schema_version": "1", "items": app.state.store.list(limit=None)},
            headers={"Content-Disposition": 'attachment; filename="learning-log.json"'},
        )

    @app.get("/")
    def index():
        return {
            "service": "biomed-evidence",
            "mode": "synthetic-demo",
            "ui": "/tools/teaching/",
        }

    return app


app = create_app()
