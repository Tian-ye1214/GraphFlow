from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.agent.turns import resume_interrupted
from app.db import get_session_factory, init_db
from app.engine.manager import resume_unfinished
from app.routers import admin, agent, auth, datasets, events, model_configs, runs, workflows

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await resume_unfinished(get_session_factory())
    await resume_interrupted(get_session_factory())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="GraphFlow", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(model_configs.router)
    app.include_router(datasets.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)
    app.include_router(agent.router)
    app.include_router(events.router)

    if STATIC_DIR.exists():  # 生产：托管前端构建产物，SPA 路由回退 index.html
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            file = STATIC_DIR / full_path
            if full_path and file.is_file():
                return FileResponse(file)
            return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
