import json
from app.models import Prompt, PromptVersion, User
from app.services import prompt_service
from sqlalchemy import func, select


async def _seed(sf):
    async with sf() as s:
        u = User(username="pu"); s.add(u); await s.commit(); return u.id


async def _versions(sf, pid):
    async with sf() as s:
        return (await s.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
                .order_by(PromptVersion.version))).scalars().all()


async def test_create_prompt_makes_v1_and_extracts_vars(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="你好 {{q}} 和 {{name}}")
    vers = await _versions(sf, pid)
    assert len(vers) == 1 and vers[0].version == 1
    assert json.loads(vers[0].variables_json) == ["name", "q"]


async def test_update_prompt_new_version_only_on_body_change(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="v1")
    async with sf() as s:
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="改名", description="d", body="v1")  # 正文没变
    assert len(await _versions(sf, pid)) == 1
    async with sf() as s:
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="改名", description="d", body="v2")  # 正文变
    assert len(await _versions(sf, pid)) == 2


async def test_rollback_copies_old_body_as_new_version(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="老")
        p = await s.get(Prompt, pid)
        await prompt_service.update_prompt(s, p, name="p", description="", body="新")
    async with sf() as s:
        assert await prompt_service.rollback_prompt(s, pid, 1) is True
    vers = await _versions(sf, pid)
    assert len(vers) == 3 and vers[-1].body == "老"
    async with sf() as s:
        assert await prompt_service.rollback_prompt(s, pid, 999) is False


async def test_delete_prompt_cascades_versions(session_factory):
    sf = session_factory; uid = await _seed(sf)
    async with sf() as s:
        pid = await prompt_service.create_prompt(s, uid, name="p", body="x")
        p = await s.get(Prompt, pid)
        await prompt_service.delete_prompt(s, p)
    async with sf() as s:
        assert await s.get(Prompt, pid) is None
        cnt = (await s.execute(select(func.count()).select_from(PromptVersion)
               .where(PromptVersion.prompt_id == pid))).scalar()
        assert cnt == 0
