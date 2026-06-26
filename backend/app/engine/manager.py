import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.engine.runner import execute_run
from app.models import Run, User


@dataclass
class RunJob:
    user_id: int
    capacity: int
    session_factory: async_sessionmaker
    prepare: Callable[[], Awaitable[bool]] | None = None


class RunManager:
    """运行任务的进程内登记处：取消事件、用户信号量、后台 asyncio.Task。"""

    def __init__(self):
        self.user_sems: dict[int, tuple[int, asyncio.Semaphore]] = {}
        self.cancel_events: dict[int, asyncio.Event] = {}
        self.done_events: dict[int, asyncio.Event] = {}
        self.tasks: dict[int, asyncio.Task] = {}
        self.queues: dict[int, deque[RunJob]] = {}
        self.active: set[int] = set()

    def user_sem(self, user_id: int, capacity: int) -> asyncio.Semaphore:
        # 连同容量缓存：容量变化时重建信号量，使 max_llm_concurrency 配置变更下一次提交即生效
        # （原实现首建即冻结，改并发度须重启进程才生效）；同容量复用，保持单一并发上限。
        cached = self.user_sems.get(user_id)
        if cached is None or cached[0] != capacity:
            self.user_sems[user_id] = (capacity, asyncio.Semaphore(capacity))
        return self.user_sems[user_id][1]

    def _enqueue(self, run_id: int, job: RunJob) -> dict:
        queue = self.queues.setdefault(run_id, deque())
        running = run_id in self.tasks and not self.tasks[run_id].done()
        position = len(queue) + (1 if run_id in self.active else 0)
        queue.append(job)
        if not running:
            self.done_events[run_id] = asyncio.Event()
            self.tasks[run_id] = asyncio.create_task(self._drain(run_id))
        return {"queued": position > 0, "position": position}

    def submit(self, run_id: int, user_id: int, capacity: int,
               session_factory: async_sessionmaker) -> dict:
        return self._enqueue(run_id, RunJob(user_id, capacity, session_factory))

    def submit_prepared(self, run_id: int, user_id: int, capacity: int,
                        session_factory: async_sessionmaker,
                        prepare: Callable[[], Awaitable[bool]]) -> dict:
        return self._enqueue(run_id, RunJob(user_id, capacity, session_factory, prepare))

    async def _drain(self, run_id: int) -> None:
        try:
            while self.queues.get(run_id):
                job = self.queues[run_id].popleft()
                ev = asyncio.Event()
                self.cancel_events[run_id] = ev
                should_run = True
                self.active.add(run_id)
                try:
                    if job.prepare is not None:
                        should_run = await job.prepare()
                    if should_run and not ev.is_set():
                        await execute_run(
                            run_id,
                            job.session_factory,
                            self.user_sem(job.user_id, job.capacity),
                            ev,
                        )
                finally:
                    self.active.discard(run_id)
                self.cancel_events.pop(run_id, None)
        finally:
            done = self.done_events.get(run_id)
            if done is not None:
                done.set()
            self._cleanup(run_id)

    def _cleanup(self, run_id: int) -> None:
        self.cancel_events.pop(run_id, None)
        self.done_events.pop(run_id, None)
        self.tasks.pop(run_id, None)
        self.queues.pop(run_id, None)
        self.active.discard(run_id)

    def cancel(self, run_id: int) -> None:
        self.queues.get(run_id, deque()).clear()
        ev = self.cancel_events.get(run_id)
        if ev:
            ev.set()

    async def wait(self, run_id: int) -> None:
        """等待某次运行到达终态。未知/已结束 run 立即返回。"""
        done = self.done_events.get(run_id)
        if done is not None:
            await done.wait()


manager = RunManager()


async def resume_unfinished(session_factory: async_sessionmaker) -> int:
    """进程启动时恢复 queued/running 的运行（断点续跑）。返回恢复数量。"""
    async with session_factory() as s:
        rows = (await s.execute(
            select(Run, User).join(User, Run.user_id == User.id)
            .where(Run.status.in_(("queued", "running")))
        )).all()
    for run, user in rows:
        manager.submit(run.id, user.id, user.max_llm_concurrency, session_factory)
    return len(rows)
