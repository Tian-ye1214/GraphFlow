from app import crypto
from app.models import ModelConfig, User
from app.services import model_service


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="mu"); s.add(u); await s.commit(); return u.id


async def test_create_model_encrypts_key(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(
            s, uid, name="m", model_name="gpt", base_url="http://x", api_key="sk-secret")
        mc_id = mc.id
    async with sf() as s:
        got = await s.get(ModelConfig, mc_id)
        assert got.api_key_enc and got.api_key_enc != "sk-secret"   # 已加密
        assert crypto.decrypt(got.api_key_enc) == "sk-secret"


async def test_update_model_blank_key_keeps_existing(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(s, uid, name="m", model_name="g", base_url="u", api_key="sk-1")
        enc1 = mc.id
    async with sf() as s:
        mc = await s.get(ModelConfig, enc1)
        before = mc.api_key_enc
        await model_service.update_model(s, mc, name="m2", model_name="g", base_url="u",
                                         provider="openai", azure_api_mode="legacy",
                                         api_version="", default_params={}, api_key="")
    async with sf() as s:
        got = await s.get(ModelConfig, enc1)
        assert got.name == "m2" and got.api_key_enc == before   # 名改了、密钥未动


async def test_delete_model(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        mc = await model_service.create_model(s, uid, name="m", model_name="g", base_url="u")
        mid = mc.id
        await model_service.delete_model(s, mc)
    async with sf() as s:
        assert await s.get(ModelConfig, mid) is None
