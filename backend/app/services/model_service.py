"""模型配置写入服务单点：REST 路由与 Agent ModelToolkit 共用，密钥加密收口于此。"""
import json

from app import crypto
from app.events import publish
from app.models import ModelConfig


async def create_model(session, user_id: int, *, name: str, model_name: str, base_url: str,
                       provider: str = "openai", azure_api_mode: str = "legacy",
                       api_version: str = "", api_key: str = "",
                       default_params: dict | None = None) -> ModelConfig:
    mc = ModelConfig(
        user_id=user_id, name=name, model_name=model_name, base_url=base_url,
        provider=provider, azure_api_mode=azure_api_mode, api_version=api_version,
        api_key_enc=crypto.encrypt(api_key) if api_key else "",
        default_params_json=json.dumps(default_params or {}, ensure_ascii=False))
    session.add(mc)
    await session.commit()
    publish(user_id, "model", mc.id)
    return mc


async def update_model(session, mc: ModelConfig, *, name: str, model_name: str, base_url: str,
                       provider: str, azure_api_mode: str, api_version: str,
                       default_params: dict, api_key: str = "") -> ModelConfig:
    mc.name, mc.model_name, mc.base_url = name, model_name, base_url
    mc.provider, mc.azure_api_mode, mc.api_version = provider, azure_api_mode, api_version
    mc.default_params_json = json.dumps(default_params, ensure_ascii=False)
    if api_key:                       # 留空=不改既有密钥
        mc.api_key_enc = crypto.encrypt(api_key)
    await session.commit()
    publish(mc.user_id, "model", mc.id)
    return mc


async def delete_model(session, mc: ModelConfig) -> None:
    uid, mid = mc.user_id, mc.id
    await session.delete(mc)
    await session.commit()
    publish(uid, "model", mid)
