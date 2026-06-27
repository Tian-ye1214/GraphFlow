"""Agent 提示词库写工具：复用 prompt_service 单点(版本化语义)。list/get 复用 catalog，不重复。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.models import Prompt, PromptVersion
from app.services import prompt_service


class PromptToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned(self, s, pid: int):
        p = await s.get(Prompt, int(pid))
        return p if p is not None and p.user_id == self._uid else None

    async def create_prompt(self, name: str, body: str, description: str = "") -> str:
        """新建库提示词(自动建 v1、提取 {{变量}})。
        Parameters:
            name: 提示词名
            body: 正文(可含 {{变量}} 占位符)
            description: 描述
        """
        try:
            async with self._sf() as s:
                pid = await prompt_service.create_prompt(
                    s, self._uid, name=name, description=description, body=body)
            return f"已创建提示词「{name}」(#{pid})"
        except Exception as e:
            return f"Error: {e}"

    async def update_prompt(self, prompt_id: int, body: str | None = None,
                            name: str | None = None, description: str | None = None) -> str:
        """改库提示词(仅正文变更才出新版本；名/描述原地改)。
        Parameters:
            prompt_id: 提示词 id
            body: 新正文(留空=保持当前正文不出新版本)
            name: 新名(留空=保持原名)
            description: 新描述(留空=保持原描述)
        """
        try:
            async with self._sf() as s:
                p = await self._owned(s, prompt_id)
                if p is None:
                    return "提示词不存在"
                cur = await prompt_service._latest(s, p.id)
                await prompt_service.update_prompt(
                    s, p,
                    name=name if name is not None else p.name,
                    description=description if description is not None else p.description,
                    body=body if body is not None else cur.body,
                )
            return f"已更新提示词 #{prompt_id}"
        except Exception as e:
            return f"Error: {e}"

    async def delete_prompt(self, prompt_id: int) -> str:
        """删除库提示词(级联删全部版本)。需用户确认。
        Parameters:
            prompt_id: 提示词 id
        """
        try:
            async with self._sf() as s:
                p = await self._owned(s, prompt_id)
                if p is None:
                    return "提示词不存在"
                if not self._confirm_delete:
                    return (
                        "删除提示词需用户确认：请向用户说明将删除提示词及其全部版本，"
                        f"在回复末尾单独一行输出 [confirm_delete] gf prompt rm {prompt_id}，然后结束回合等待确认。"
                    )
                await prompt_service.delete_prompt(s, p)
            return f"已删除提示词 #{prompt_id}"
        except Exception as e:
            return f"Error: {e}"

    async def list_prompt_versions(self, prompt_id: int) -> str:
        """列某提示词的全部版本(版本号/正文摘要)。
        Parameters:
            prompt_id: 提示词 id
        """
        try:
            async with self._sf() as s:
                p = await self._owned(s, prompt_id)
                if p is None:
                    return json.dumps({"error": "prompt_not_found"}, ensure_ascii=False)
                vers = (await s.execute(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == p.id)
                    .order_by(PromptVersion.version)
                )).scalars().all()
            return json.dumps(
                _fit_budget({"rows": [
                    {"version": v.version, "body": v.body[:200]} for v in vers
                ]}),
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    async def rollback_prompt(self, prompt_id: int, version: int) -> str:
        """把提示词回滚到历史版本(复制其正文成新版)。
        Parameters:
            prompt_id: 提示词 id
            version: 目标版本号
        """
        try:
            async with self._sf() as s:
                p = await self._owned(s, prompt_id)
                if p is None:
                    return "提示词不存在"
                ok = await prompt_service.rollback_prompt(s, p.id, int(version))
            if not ok:
                return f"Error: 版本 {version} 不存在"
            return f"已回滚提示词 #{prompt_id} 到版本 {version}"
        except Exception as e:
            return f"Error: {e}"

    async def duplicate_prompt(self, prompt_id: int, name: str | None = None) -> str:
        """复制库提示词为新提示词。
        Parameters:
            prompt_id: 源提示词 id
            name: 新名(留空=原名+副本)
        """
        try:
            async with self._sf() as s:
                src = await self._owned(s, prompt_id)
                if src is None:
                    return "提示词不存在"
                new_id = await prompt_service.duplicate_prompt(s, src, name)
            return f"已复制为新提示词 #{new_id}"
        except Exception as e:
            return f"Error: {e}"

    @property
    def tools(self) -> list:
        return [
            self.create_prompt,
            self.update_prompt,
            self.delete_prompt,
            self.list_prompt_versions,
            self.rollback_prompt,
            self.duplicate_prompt,
        ]
