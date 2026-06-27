"""提示词写入服务单点：版本化语义（正文变更才出新版）收口于此，REST 与 Agent PromptToolkit 共用。"""
import json

from sqlalchemy import delete as sa_delete, select

from app.engine.nodes import TEMPLATE_RE
from app.events import publish
from app.models import Prompt, PromptVersion


def extract_vars(body: str) -> list[str]:
    return sorted({m.group(1) for m in TEMPLATE_RE.finditer(body or "")})


async def _latest(session, pid: int) -> PromptVersion:
    return (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
            .order_by(PromptVersion.version.desc()).limit(1))).scalar_one()


async def create_prompt(session, user_id: int, *, name: str, description: str = "",
                        body: str = "") -> int:
    p = Prompt(user_id=user_id, name=name, description=description)
    session.add(p)
    await session.flush()
    session.add(PromptVersion(prompt_id=p.id, version=1, body=body,
                              variables_json=json.dumps(extract_vars(body), ensure_ascii=False)))
    await session.commit()
    publish(user_id, "prompt", p.id)
    return p.id


async def update_prompt(session, prompt: Prompt, *, name: str, description: str, body: str) -> None:
    prompt.name, prompt.description = name, description
    cur = await _latest(session, prompt.id)
    if body != cur.body:    # 仅正文变化才追加新版本；名/描述原地改
        session.add(PromptVersion(prompt_id=prompt.id, version=cur.version + 1, body=body,
                                  variables_json=json.dumps(extract_vars(body), ensure_ascii=False)))
    await session.commit()
    publish(prompt.user_id, "prompt", prompt.id)


async def delete_prompt(session, prompt: Prompt) -> None:
    uid, pid = prompt.user_id, prompt.id
    await session.execute(sa_delete(PromptVersion).where(PromptVersion.prompt_id == pid))
    await session.delete(prompt)
    await session.commit()
    publish(uid, "prompt", pid)


async def rollback_prompt(session, prompt_id: int, version: int) -> bool:
    target = (await session.execute(select(PromptVersion).where(
        PromptVersion.prompt_id == prompt_id, PromptVersion.version == version))).scalar_one_or_none()
    if target is None:
        return False
    cur = await _latest(session, prompt_id)
    session.add(PromptVersion(prompt_id=prompt_id, version=cur.version + 1,
                              body=target.body, variables_json=target.variables_json))
    await session.commit()
    p = await session.get(Prompt, prompt_id)
    publish(p.user_id, "prompt", prompt_id)
    return True


async def duplicate_prompt(session, src: Prompt, new_name: str | None = None) -> int:
    cur = await _latest(session, src.id)
    new = Prompt(user_id=src.user_id, name=new_name or f"{src.name} 副本", description=src.description)
    session.add(new)
    await session.flush()
    session.add(PromptVersion(prompt_id=new.id, version=1, body=cur.body,
                              variables_json=cur.variables_json))
    await session.commit()
    publish(src.user_id, "prompt", new.id)
    return new.id
