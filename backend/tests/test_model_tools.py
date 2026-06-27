from sqlalchemy import select

from app import crypto
from app.agent.model_tools import ModelToolkit
from app.models import ModelConfig, User


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="mt"); s.add(u); await s.commit(); return u.id


async def test_create_model_tool(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    msg = await ModelToolkit(sf, uid).create_model(
        name="m1", base_url="http://x", model_name="gpt", api_key="sk-z")
    async with sf() as s:
        mc = (await s.execute(select(ModelConfig))).scalars().first()
        assert mc.name == "m1" and crypto.decrypt(mc.api_key_enc) == "sk-z"
    assert "已创建" in msg and "sk-z" not in msg   # 返回串不回显密钥


async def test_delete_model_requires_confirmation(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        mc = ModelConfig(user_id=uid, name="m", model_name="g", base_url="u",
                         api_key_enc="", default_params_json="{}"); s.add(mc); await s.commit(); mid = mc.id
    msg = await ModelToolkit(sf, uid).delete_model(mid)
    assert "确认" in msg
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is not None
    msg2 = await ModelToolkit(sf, uid, confirm_delete=True).delete_model(mid)
    assert "已删除" in msg2
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is None


async def test_model_tool_cross_tenant(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        mc = ModelConfig(user_id=uid, name="m", model_name="g", base_url="u",
                         api_key_enc="", default_params_json="{}"); s.add(mc); await s.commit(); mid = mc.id
    assert "不存在" in await ModelToolkit(sf, uid + 999).delete_model(mid)
