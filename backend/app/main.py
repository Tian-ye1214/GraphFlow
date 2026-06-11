from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.db import get_session_factory, init_db
from app.engine.manager import resume_unfinished
from app.routers import auth, datasets, model_configs, runs, workflows


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await resume_unfinished(get_session_factory())
    yield
    await db.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="GraphFlow", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(model_configs.router)
    app.include_router(datasets.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)
    return app


app = create_app()
