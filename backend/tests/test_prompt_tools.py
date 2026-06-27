"""PromptToolkit TDD 测试：create/update/delete门禁/list_versions/rollback/duplicate。"""
import json
import re

import pytest

from app.agent.prompt_tools import PromptToolkit
from app.models import Prompt, PromptVersion, User
from sqlalchemy import func, select


async def _seed_user(sf, username="pt_user"):
    async with sf() as s:
        u = User(username=username)
        s.add(u)
        await s.commit()
        return u.id


async def test_create_and_list_versions(session_factory):
    sf = session_factory
    uid = await _seed_user(sf)
    msg = await PromptToolkit(sf, uid).create_prompt(name="p", body="你好 {{q}}")
    pid = int(re.search(r"#(\d+)", msg).group(1))
    vers = json.loads(await PromptToolkit(sf, uid).list_prompt_versions(pid))
    assert vers["rows"][0]["version"] == 1


async def test_delete_prompt_requires_confirmation(session_factory):
    sf = session_factory
    uid = await _seed_user(sf, "pt_del")
    async with sf() as s:
        p = Prompt(user_id=uid, name="p", description="")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="x", variables_json="[]"))
        await s.commit()
        pid = p.id
    # 未确认：返回含「确认」的串，不执行删除
    result = await PromptToolkit(sf, uid).delete_prompt(pid)
    assert "确认" in result
    async with sf() as s:
        assert await s.get(Prompt, pid) is not None
    # 确认后：执行删除
    result2 = await PromptToolkit(sf, uid, confirm_delete=True).delete_prompt(pid)
    assert "已删除" in result2
    async with sf() as s:
        assert await s.get(Prompt, pid) is None


async def test_rollback_tool(session_factory):
    sf = session_factory
    uid = await _seed_user(sf, "pt_rb")
    async with sf() as s:
        p = Prompt(user_id=uid, name="p", description="")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="老", variables_json="[]"))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="新", variables_json="[]"))
        await s.commit()
        pid = p.id
    result = await PromptToolkit(sf, uid).rollback_prompt(pid, 1)
    assert "已回滚" in result
    async with sf() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(PromptVersion)
            .where(PromptVersion.prompt_id == pid)
        )).scalar()
        assert cnt == 3


async def test_rollback_version_not_found(session_factory):
    sf = session_factory
    uid = await _seed_user(sf, "pt_rb2")
    async with sf() as s:
        p = Prompt(user_id=uid, name="p", description="")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="x", variables_json="[]"))
        await s.commit()
        pid = p.id
    result = await PromptToolkit(sf, uid).rollback_prompt(pid, 99)
    assert "Error" in result and "99" in result


async def test_update_prompt(session_factory):
    sf = session_factory
    uid = await _seed_user(sf, "pt_upd")
    async with sf() as s:
        p = Prompt(user_id=uid, name="orig", description="desc")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="body1", variables_json="[]"))
        await s.commit()
        pid = p.id
    # 更新正文 → 出新版本
    result = await PromptToolkit(sf, uid).update_prompt(pid, body="body2")
    assert "已更新" in result
    async with sf() as s:
        cnt = (await s.execute(
            select(func.count()).select_from(PromptVersion)
            .where(PromptVersion.prompt_id == pid)
        )).scalar()
        assert cnt == 2
    # 留空 body → 不出新版本（用既有正文）
    result2 = await PromptToolkit(sf, uid).update_prompt(pid, name="new_name")
    assert "已更新" in result2
    async with sf() as s:
        p2 = await s.get(Prompt, pid)
        assert p2.name == "new_name"
        cnt2 = (await s.execute(
            select(func.count()).select_from(PromptVersion)
            .where(PromptVersion.prompt_id == pid)
        )).scalar()
        assert cnt2 == 2  # 正文未变，无新版本


async def test_duplicate_prompt(session_factory):
    sf = session_factory
    uid = await _seed_user(sf, "pt_dup")
    async with sf() as s:
        p = Prompt(user_id=uid, name="src", description="d")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="hello", variables_json="[]"))
        await s.commit()
        pid = p.id
    result = await PromptToolkit(sf, uid).duplicate_prompt(pid, name="copy")
    assert "已复制" in result
    new_pid = int(re.search(r"#(\d+)", result).group(1))
    async with sf() as s:
        np = await s.get(Prompt, new_pid)
        assert np is not None and np.name == "copy"
        nv = (await s.execute(
            select(PromptVersion).where(PromptVersion.prompt_id == new_pid)
        )).scalar_one()
        assert nv.body == "hello"


async def test_cross_tenant_not_found(session_factory):
    """跨租户：他人提示词返回不存在。"""
    sf = session_factory
    uid1 = await _seed_user(sf, "pt_ct1")
    uid2 = await _seed_user(sf, "pt_ct2")
    async with sf() as s:
        p = Prompt(user_id=uid1, name="secret", description="")
        s.add(p)
        await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="x", variables_json="[]"))
        await s.commit()
        pid = p.id
    # uid2 无权访问 uid1 的提示词
    res_del = await PromptToolkit(sf, uid2, confirm_delete=True).delete_prompt(pid)
    assert "不存在" in res_del
    res_rb = await PromptToolkit(sf, uid2).rollback_prompt(pid, 1)
    assert "不存在" in res_rb
    res_dup = await PromptToolkit(sf, uid2).duplicate_prompt(pid)
    assert "不存在" in res_dup
    res_lst = json.loads(await PromptToolkit(sf, uid2).list_prompt_versions(pid))
    assert res_lst.get("error") == "prompt_not_found"


async def test_list_versions_fit_budget(session_factory):
    """list_prompt_versions 正常返回 rows 列表。"""
    sf = session_factory
    uid = await _seed_user(sf, "pt_lv")
    msg = await PromptToolkit(sf, uid).create_prompt(name="lv", body="{{x}} test")
    pid = int(re.search(r"#(\d+)", msg).group(1))
    result = json.loads(await PromptToolkit(sf, uid).list_prompt_versions(pid))
    assert "rows" in result
    assert result["rows"][0]["version"] == 1
    assert "body" in result["rows"][0]
