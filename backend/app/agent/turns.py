"""AgentTurnManager：后台回合执行、目标模式自动续轮、停止与重启恢复。"""
import asyncio
import json
import re
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter
from sqlalchemy import select

from app.agent.goal import parse_goal
from app.agent.system import AgentSystem
from app.agent.tools import EMIT
from app.config import settings
from app.db import get_session_factory
from app.events import publish
from app.models import AgentMessage, AgentSession, ModelConfig, User


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def session_dir(username: str, session_id: int) -> Path:
    # 必须绝对：相对路径会被 gf 子进程按其 cwd 二次拼接（GF_STATE_FILE 失效→Agent 自行 login 成幽灵用户）
    return (settings.data_dir / "agent" / _safe(username) / str(session_id)).resolve()


class AgentTurnManager:
    def __init__(self):
        self.tasks: dict[int, asyncio.Task] = {}
        self.stop_flags: set[int] = set()

    def submit(self, session_id: int, user_id: int, text: str) -> None:
        self.stop_flags.discard(session_id)
        task = asyncio.create_task(self._run_turn(session_id, user_id, text))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    def request_stop(self, session_id: int) -> None:
        self.stop_flags.add(session_id)

    def cancel(self, session_id: int) -> None:
        task = self.tasks.get(session_id)
        if task:
            task.cancel()

    def _make_emit(self, session_id: int, user_id: int):
        async def emit(kind: str, data=None):
            if kind == "tool_end":  # 工具消息落库先于 publish（spec §7）
                async with get_session_factory()() as s:
                    s.add(AgentMessage(session_id=session_id, role="tool",
                                       content_json=json.dumps(data, ensure_ascii=False)))
                    await s.commit()
            publish(user_id, "agent", session_id, kind=kind, data=data)
        return emit

    async def _add_message(self, session_id: int, user_id: int, role: str, content: dict) -> None:
        async with get_session_factory()() as s:
            s.add(AgentMessage(session_id=session_id, role=role,
                               content_json=json.dumps(content, ensure_ascii=False)))
            await s.commit()
        publish(user_id, "agent", session_id, kind="message")

    async def _run_turn(self, session_id: int, user_id: int, text: str) -> None:
        sf = get_session_factory()
        async with sf() as s:
            sess = await s.get(AgentSession, session_id)
            history = ModelMessagesTypeAdapter.validate_json(sess.history_json)
            models = {role: await s.get(ModelConfig, mid)
                      for role, mid in json.loads(sess.models_json).items()}
            models.setdefault("compactor", models.get("coordinator"))
            user = await s.get(User, user_id)
            username = user.username
        emit = self._make_emit(session_id, user_id)
        EMIT.set(emit)
        system = AgentSystem(models=models, workdir=session_dir(username, session_id),
                             confirm_delete=text.startswith("确认"), emit=emit)
        rounds, capped, input_text = 0, False, text
        try:
            while True:
                history, output = await system.run_turn(input_text, history)
                signal, cleaned = parse_goal(output)
                await self._add_message(session_id, user_id, "assistant", {"text": cleaned})
                if capped or signal != "CONTINUE":
                    break
                if session_id in self.stop_flags:
                    await self._add_message(session_id, user_id, "assistant",
                                            {"text": f"目标模式已被用户停止（第 {rounds + 1} 轮）"})
                    break
                if rounds >= settings.agent_goal_max_rounds:
                    capped = True
                    input_text = (f"已达自动续轮上限（{settings.agent_goal_max_rounds} 轮），"
                                  "请总结当前进展并结束本回合，等待用户决定是否继续。")
                    continue
                rounds += 1
                publish(user_id, "agent", session_id, kind="goal_round", data=rounds)
                input_text = "继续推进目标"
        except Exception as e:
            await self._add_message(session_id, user_id, "assistant", {"text": f"执行出错: {e}"})
        finally:
            async with sf() as s:
                sess = await s.get(AgentSession, session_id)
                if sess is not None:  # 会话可能在回合中被删除（任务被 cancel）
                    sess.history_json = ModelMessagesTypeAdapter.dump_json(history).decode()
                    sess.status = "idle"
                    await s.commit()
            publish(user_id, "agent", session_id, kind="turn_done")


    def submit_goal(self, session_id: int, user_id: int, workflow_id: int, goal_text: str) -> None:
        self.stop_flags.discard(session_id)
        task = asyncio.create_task(self._run_goal(session_id, user_id, workflow_id, goal_text))
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    async def _run_goal(self, session_id, user_id, workflow_id, goal_text):
        from app.agent import goal_loop as gl
        from app.engine.manager import manager
        from app.services import run_service as rs
        sf = get_session_factory()
        async with sf() as s:
            sess = await s.get(AgentSession, session_id)
            history = ModelMessagesTypeAdapter.validate_json(sess.history_json)
            models = {role: await s.get(ModelConfig, mid)
                      for role, mid in json.loads(sess.models_json).items()}
            models.setdefault("compactor", models.get("coordinator"))
            user = await s.get(User, user_id)
            username = user.username
        emit = self._make_emit(session_id, user_id)
        EMIT.set(emit)
        system = AgentSystem(models=models, workdir=session_dir(username, session_id),
                             confirm_delete=False, emit=emit)
        threshold = rs.parse_threshold(goal_text)
        best, no_improve, round_i = -1.0, 0, 0
        input_text = gl.first_round_prompt(goal_text)
        try:
            while True:
                history, output = await system.run_turn(input_text, history)
                signal, cleaned = parse_goal(output)
                await self._add_message(session_id, user_id, "assistant", {"text": cleaned})
                if signal == "DONE" or session_id in self.stop_flags:
                    break
                run_id = await rs.enqueue_run(sf, user_id, workflow_id)
                round_i += 1
                publish(user_id, "agent", session_id, kind="goal_round", data=round_i)
                await manager.wait(run_id)
                metric = await rs.first_round_rate(sf, run_id)
                publish(user_id, "agent", session_id, kind="goal_metric",
                        data={"round": round_i, "metric": metric, "run_id": run_id})
                d = gl.decide(metric=metric, threshold=threshold, best=best,
                              no_improve=no_improve, no_improve_k=settings.goal_no_improve_k)
                best, no_improve = d.new_best, d.new_no_improve
                if d.stop:
                    await self._add_message(session_id, user_id, "assistant", {"text": d.reason})
                    break
                if round_i >= settings.agent_goal_max_rounds:
                    await self._add_message(session_id, user_id, "assistant",
                                            {"text": f"已达轮数兜底上限（{settings.agent_goal_max_rounds}）"})
                    break
                failures = await rs.sample_failures(sf, run_id, n=20)
                input_text = gl.build_round_prompt(goal_text, metric, failures, run_id)
        except Exception as e:
            await self._add_message(session_id, user_id, "assistant", {"text": f"目标模式出错: {e}"})
        finally:
            async with sf() as s:
                sess = await s.get(AgentSession, session_id)
                if sess is not None:
                    sess.history_json = ModelMessagesTypeAdapter.dump_json(history).decode()
                    sess.status = "idle"
                    await s.commit()
            publish(user_id, "agent", session_id, kind="turn_done")


turn_manager = AgentTurnManager()


async def resume_interrupted(session_factory) -> int:
    """进程启动时把 running 会话重置为 idle 并补一条中断说明（回合内存态无法续跑）。"""
    async with session_factory() as s:
        rows = (await s.execute(select(AgentSession).where(
            AgentSession.status == "running"))).scalars().all()
        for sess in rows:
            sess.status = "idle"
            s.add(AgentMessage(session_id=sess.id, role="assistant",
                               content_json=json.dumps({"text": "回合因服务重启中断"},
                                                       ensure_ascii=False)))
        await s.commit()
    return len(rows)
