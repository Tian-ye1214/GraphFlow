"""Agent 模型配置写工具：复用 model_service 单点；api_key 为顶层参数(经 _brief 打码)。"""
import json

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import ModelConfig
from app.services import llm, model_service


class ModelToolkit:
    def __init__(self, session_factory: async_sessionmaker, user_id: int,
                 confirm_delete: bool = False):
        self._sf = session_factory
        self._uid = user_id
        self._confirm_delete = confirm_delete

    async def _owned(self, s, mc_id: int):
        mc = await s.get(ModelConfig, int(mc_id))
        return mc if mc is not None and mc.user_id == self._uid else None

    async def create_model(self, name: str, base_url: str, model_name: str,
                           api_key: str | None = None, provider: str = "openai",
                           api_version: str = "") -> str:
        """新建模型配置。api_key 会加密存储、不回显。
        Parameters:
            name: 配置名
            base_url: API 基址
            model_name: 模型 ID(如 deepseek-chat)
            api_key: 密钥(可选；留空可后续补)
            provider: openai 或 azure
            api_version: azure legacy 需填
        """
        try:
            async with self._sf() as s:
                mc = await model_service.create_model(
                    s, self._uid, name=name, model_name=model_name, base_url=base_url,
                    provider=provider, azure_api_mode="legacy", api_version=api_version,
                    api_key=api_key or "")
                return f"已创建模型「{name}」(#{mc.id})"
        except Exception as e:
            return f"Error: {e}"

    async def update_model(self, model_id: int, name: str | None = None, base_url: str | None = None,
                           model_name: str | None = None, api_key: str | None = None) -> str:
        """修改模型配置(只改给出的字段；api_key 留空=不改)。
        Parameters:
            model_id: 模型配置 id
        """
        try:
            async with self._sf() as s:
                mc = await self._owned(s, model_id)
                if mc is None:
                    return "模型配置不存在"
                await model_service.update_model(
                    s, mc, name=name if name is not None else mc.name,
                    model_name=model_name if model_name is not None else mc.model_name,
                    base_url=base_url if base_url is not None else mc.base_url,
                    provider=mc.provider, azure_api_mode=mc.azure_api_mode, api_version=mc.api_version,
                    default_params=json.loads(mc.default_params_json),
                    api_key=api_key or "")
                return f"已更新模型 #{model_id}"
        except Exception as e:
            return f"Error: {e}"

    async def delete_model(self, model_id: int) -> str:
        """删除模型配置。需用户确认。
        Parameters:
            model_id: 模型配置 id
        """
        try:
            async with self._sf() as s:
                mc = await self._owned(s, model_id)
                if mc is None:
                    return "模型配置不存在"
                if not self._confirm_delete:
                    return ("删除模型配置需用户确认：请向用户说明，"
                            f"在回复末尾单独一行输出 [confirm_delete] gf model rm {model_id}，然后结束回合等待确认。")
                await model_service.delete_model(s, mc)
                return f"已删除模型配置 #{model_id}"
        except Exception as e:
            return f"Error: {e}"

    async def test_model(self, model_id: int) -> str:
        """连通测试：真实发一条请求(会产生少量费用)。
        Parameters:
            model_id: 模型配置 id
        """
        try:
            async with self._sf() as s:
                mc = await self._owned(s, model_id)
                if mc is None:
                    return "模型配置不存在"
                try:
                    text, _ = await llm.chat(mc, "", "这是一次连通测试，没问题回复OK",
                                             params={"max_tokens": 65536}, retries=1)
                    return f"连通正常：{text[:100]}"
                except llm.LLMError as e:
                    return f"连通失败：{e}"
        except Exception as e:
            return f"Error: {e}"

    @property
    def tools(self) -> list:
        return [self.create_model, self.update_model, self.delete_model, self.test_model]
