"""CatalogTools 直接单测：数据集/模型/提示词库清单；密钥不泄漏；租户隔离。"""
import json

from app.agent.catalog import CatalogTools
from app.models import Dataset, ModelConfig, Prompt, PromptVersion, User


async def _seed(sf):
    async with sf() as s:
        u = User(username="tester"); s.add(u); await s.flush()
        s.add(Dataset(user_id=u.id, name="集A", source="upload", row_count=3,
                      columns_json=json.dumps(["q"])))
        s.add(ModelConfig(user_id=u.id, name="通义", model_name="qwen", base_url="http://x/v1",
                          provider="openai", api_key_enc="ENCKEY", default_params_json="{}"))
        s.add(ModelConfig(user_id=u.id, name="无钥", model_name="m", base_url="http://y/v1",
                          provider="openai", api_key_enc="", default_params_json="{}"))
        p = Prompt(user_id=u.id, name="翻译模板", description="把列翻译"); s.add(p); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="翻译 {{q}}", variables_json=json.dumps(["q"])))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="精翻 {{q}}", variables_json=json.dumps(["q"])))
        ids = (u.id, p.id)
        await s.commit()
    return ids


async def test_list_user_datasets(session_factory):
    sf = session_factory
    uid, _ = await _seed(sf)
    out = json.loads(await CatalogTools(sf, uid).list_user_datasets())
    assert out["rows"][0]["name"] == "集A" and out["rows"][0]["columns"] == ["q"]


async def test_list_user_models_never_leaks_key(session_factory):
    sf = session_factory
    uid, _ = await _seed(sf)
    raw = await CatalogTools(sf, uid).list_user_models()
    out = json.loads(raw)
    by = {m["name"]: m for m in out["rows"]}
    assert by["通义"]["api_key_set"] is True and by["无钥"]["api_key_set"] is False
    assert "ENCKEY" not in raw and "api_key_enc" not in raw   # 绝不泄漏密钥


async def test_prompts_list_and_get_latest_version(session_factory):
    sf = session_factory
    uid, pid = await _seed(sf)
    lst = json.loads(await CatalogTools(sf, uid).list_prompts())
    assert lst["rows"][0]["name"] == "翻译模板" and lst["rows"][0]["latest_version"] == 2
    g = json.loads(await CatalogTools(sf, uid).get_prompt(pid))
    assert g["version"] == 2 and g["body"] == "精翻 {{q}}" and g["variables"] == ["q"]


async def test_catalog_tenant_isolated(session_factory):
    sf = session_factory
    uid, pid = await _seed(sf)
    assert json.loads(await CatalogTools(sf, uid + 999).list_user_datasets())["rows"] == []
    assert json.loads(await CatalogTools(sf, uid + 999).get_prompt(pid)).get("error") == "prompt_not_found"
