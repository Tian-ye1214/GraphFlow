import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.db import get_session
from app.models import ModelCallLog, User

router = APIRouter(prefix="/api/model-logs", tags=["model-logs"])


def _out(r: ModelCallLog) -> dict:
    return {"id": r.id, "source": r.source, "node_id": r.node_id, "run_id": r.run_id,
            "workflow_id": r.workflow_id, "session_id": r.session_id,
            "model_name": r.model_name, "provider": r.provider,
            "request": json.loads(r.request_json or "[]"), "response": r.response_json,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens,
            "created_at": r.created_at.isoformat()}


@router.get("")
async def list_model_logs(source: str | None = None, run_id: int | None = None,
                          node_id: str | None = None, limit: int = 100, offset: int = 0,
                          user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    stmt = select(ModelCallLog).where(ModelCallLog.user_id == user.id)
    if source is not None:
        stmt = stmt.where(ModelCallLog.source == source)
    if run_id is not None:
        stmt = stmt.where(ModelCallLog.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(ModelCallLog.node_id == node_id)
    rows = (await session.execute(
        stmt.order_by(ModelCallLog.id.desc()).offset(offset).limit(min(max(limit, 0), 500)))).scalars().all()
    return [_out(r) for r in rows]
