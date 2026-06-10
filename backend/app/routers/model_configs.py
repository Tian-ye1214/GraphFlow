import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.auth import get_current_user
from app.db import get_session
from app.models import ModelConfig, User
from app.services import llm

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelConfigIn(BaseModel):
    name: str
    model_name: str
    base_url: str
    api_key: str = ""
    default_params: dict = {}


def _out(mc: ModelConfig) -> dict:
    return {
        "id": mc.id,
        "name": mc.name,
        "model_name": mc.model_name,
        "base_url": mc.base_url,
        "api_key_set": bool(mc.api_key_enc),
        "default_params": json.loads(mc.default_params_json),
    }


async def _get_owned(mc_id: int, user: User, session: AsyncSession) -> ModelConfig:
    mc = await session.get(ModelConfig, mc_id)
    if mc is None or mc.user_id != user.id:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    return mc


@router.get("")
async def list_models(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(ModelConfig).where(ModelConfig.user_id == user.id).order_by(ModelConfig.id)
    )).scalars().all()
    return [_out(m) for m in rows]


@router.post("")
async def create_model(body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = ModelConfig(
        user_id=user.id, name=body.name, model_name=body.model_name, base_url=body.base_url,
        api_key_enc=crypto.encrypt(body.api_key) if body.api_key else "",
        default_params_json=json.dumps(body.default_params, ensure_ascii=False),
    )
    session.add(mc)
    await session.commit()
    return _out(mc)


@router.put("/{mc_id}")
async def update_model(mc_id: int, body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    mc.name, mc.model_name, mc.base_url = body.name, body.model_name, body.base_url
    mc.default_params_json = json.dumps(body.default_params, ensure_ascii=False)
    if body.api_key:  # 留空表示不修改 key
        mc.api_key_enc = crypto.encrypt(body.api_key)
    await session.commit()
    return _out(mc)


@router.delete("/{mc_id}")
async def delete_model(mc_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    await session.delete(mc)
    await session.commit()
    return {"ok": True}


@router.post("/{mc_id}/test")
async def test_model(mc_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    try:
        text, _ = await llm.chat(mc, "", "ping", params={"max_tokens": 8}, retries=1)
        return {"ok": True, "reply": text[:100]}
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
