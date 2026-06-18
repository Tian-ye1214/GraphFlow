import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.auth import get_current_user
from app.db import get_session
from app.events import publish
from app.models import ModelConfig, User
from app.services import llm

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelConfigIn(BaseModel):
    name: str
    model_name: str
    base_url: str
    provider: Literal["openai", "azure"] = "openai"
    azure_api_mode: Literal["legacy", "v1"] = "legacy"
    api_version: str = ""
    api_key: str = ""
    default_params: dict = Field(default_factory=dict)


def _out(mc: ModelConfig) -> dict:
    provider = getattr(mc, "provider", None) or "openai"
    return {
        "id": mc.id,
        "name": mc.name,
        "model_name": mc.model_name,
        "base_url": mc.base_url,
        "provider": provider,
        "azure_api_mode": (getattr(mc, "azure_api_mode", None) or "legacy") if provider == "azure" else "legacy",
        "api_version": (getattr(mc, "api_version", None) or "") if provider == "azure" else "",
        "api_key_set": bool(mc.api_key_enc),
        "default_params": json.loads(mc.default_params_json),
    }


def _validated_provider_fields(body: ModelConfigIn, *, existing_key: str = "") -> tuple[str, str, str]:
    provider = body.provider
    azure_api_mode = body.azure_api_mode if provider == "azure" else "legacy"
    api_version = body.api_version.strip()
    if provider == "azure":
        if azure_api_mode == "legacy" and not api_version:
            raise HTTPException(status_code=400, detail="Azure 模型必须配置 API Version")
        if azure_api_mode == "v1" and api_version:
            raise HTTPException(status_code=400, detail="Azure v1 API 不能配置 API Version")
        if not body.api_key and not existing_key:
            raise HTTPException(status_code=400, detail="Azure 模型必须配置 API Key")
        return provider, api_version if azure_api_mode == "legacy" else "", azure_api_mode
    return provider, "", "legacy"


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
    provider, api_version, azure_api_mode = _validated_provider_fields(body)
    mc = ModelConfig(
        user_id=user.id, name=body.name, model_name=body.model_name, base_url=body.base_url,
        provider=provider, azure_api_mode=azure_api_mode, api_version=api_version,
        api_key_enc=crypto.encrypt(body.api_key) if body.api_key else "",
        default_params_json=json.dumps(body.default_params, ensure_ascii=False),
    )
    session.add(mc)
    await session.commit()
    publish(user.id, "model", mc.id)
    return _out(mc)


@router.put("/{mc_id}")
async def update_model(mc_id: int, body: ModelConfigIn, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    provider, api_version, azure_api_mode = _validated_provider_fields(body, existing_key=mc.api_key_enc)
    mc.name, mc.model_name, mc.base_url = body.name, body.model_name, body.base_url
    mc.provider, mc.azure_api_mode, mc.api_version = provider, azure_api_mode, api_version
    mc.default_params_json = json.dumps(body.default_params, ensure_ascii=False)
    if body.api_key:
        mc.api_key_enc = crypto.encrypt(body.api_key)
    await session.commit()
    publish(user.id, "model", mc.id)
    return _out(mc)


@router.delete("/{mc_id}")
async def delete_model(mc_id: int, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    await session.delete(mc)
    await session.commit()
    publish(user.id, "model", mc_id)
    return {"ok": True}


@router.post("/{mc_id}/test")
async def test_model(mc_id: int, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    mc = await _get_owned(mc_id, user, session)
    try:
        text, _ = await llm.chat(mc, "", "这是一次连通测试，没问题回复OK", params={"max_tokens": 512}, retries=1)
        return {"ok": True, "reply": text[:100]}
    except llm.LLMError as e:
        return {"ok": False, "error": str(e)}
