"""节点助手只读目录工具：本租户数据集/模型/提示词库清单。铁律：绝不返回 api_key 明文。"""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.data_preview import _fit_budget
from app.models import Dataset, ModelConfig, Prompt, PromptVersion


class CatalogTools:
    def __init__(self, session_factory: async_sessionmaker, user_id: int):
        self._sf = session_factory
        self._uid = user_id

    async def list_user_datasets(self) -> str:
        """列本租户全部数据集(id/名/行数/列名/来源)。写提示词时知道有哪些数据源、可引用哪些列。"""
        async with self._sf() as s:
            recs = (await s.execute(select(Dataset).where(Dataset.user_id == self._uid)
                                    .order_by(Dataset.id.desc()))).scalars().all()
            rows = [{"id": d.id, "name": d.name, "row_count": d.row_count,
                     "columns": json.loads(d.columns_json), "source": d.source} for d in recs]
            return json.dumps(_fit_budget({"rows": rows}), ensure_ascii=False)

    async def list_user_models(self) -> str:
        """列本租户全部模型配置(id/名/模型ID/provider/base_url/是否已配密钥)。绝不返回密钥明文。"""
        async with self._sf() as s:
            recs = (await s.execute(select(ModelConfig).where(ModelConfig.user_id == self._uid)
                                    .order_by(ModelConfig.id.desc()))).scalars().all()
            rows = [{"id": m.id, "name": m.name, "model_name": m.model_name,
                     "provider": m.provider, "base_url": m.base_url,
                     "api_key_set": bool(m.api_key_enc)} for m in recs]
            return json.dumps(_fit_budget({"rows": rows}), ensure_ascii=False)

    async def list_prompts(self) -> str:
        """列本租户提示词库(id/名/描述/最新版本号/声明变量)。配提示词前看有无可复用的。"""
        async with self._sf() as s:
            prompts = (await s.execute(select(Prompt).where(Prompt.user_id == self._uid)
                                       .order_by(Prompt.id.desc()))).scalars().all()
            rows = []
            for p in prompts:
                ver = await self._latest_version(s, p.id)
                rows.append({"id": p.id, "name": p.name, "description": p.description,
                             "latest_version": ver.version if ver else 0,
                             "variables": json.loads(ver.variables_json) if ver else []})
            return json.dumps(_fit_budget({"rows": rows}), ensure_ascii=False)

    async def get_prompt(self, prompt_id: int) -> str:
        """看某个库提示词的当前正文与声明变量(复用现成提示词时取正文)。
        Parameters:
            prompt_id: 提示词 id（从 list_prompts 获取）
        """
        async with self._sf() as s:
            p = await s.get(Prompt, int(prompt_id))
            if p is None or p.user_id != self._uid:
                return json.dumps({"error": "prompt_not_found"}, ensure_ascii=False)
            ver = await self._latest_version(s, p.id)
            return json.dumps({"id": p.id, "name": p.name, "description": p.description,
                               "version": ver.version if ver else 0,
                               "body": ver.body if ver else "",
                               "variables": json.loads(ver.variables_json) if ver else []},
                              ensure_ascii=False)

    @staticmethod
    async def _latest_version(session, prompt_id: int):
        return (await session.execute(
            select(PromptVersion).where(PromptVersion.prompt_id == prompt_id)
            .order_by(PromptVersion.version.desc()).limit(1))).scalars().first()


def make_catalog_tools(session_factory: async_sessionmaker, user_id: int) -> list:
    t = CatalogTools(session_factory, user_id)
    return [t.list_user_datasets, t.list_user_models, t.list_prompts, t.get_prompt]
