import asyncio
from app.engine.manager import RunManager


async def test_wait_fires_on_completion(monkeypatch):
    m = RunManager()
    import app.engine.manager as mod
    async def fake_exec(run_id, sf, sem, ev):
        await asyncio.sleep(0)
    monkeypatch.setattr(mod, "execute_run", fake_exec)
    m.submit(123, user_id=1, capacity=2, session_factory=None)
    await asyncio.wait_for(m.wait(123), timeout=1)   # 不挂起即通过


async def test_same_run_submissions_run_fifo(monkeypatch):
    m = RunManager()
    import app.engine.manager as mod
    active = {"count": 0, "max": 0}
    calls = []

    async def fake_exec(run_id, sf, sem, ev):
        active["count"] += 1
        active["max"] = max(active["max"], active["count"])
        try:
            calls.append(run_id)
            await asyncio.sleep(0.05)
        finally:
            active["count"] -= 1

    monkeypatch.setattr(mod, "execute_run", fake_exec)
    first = m.submit(123, user_id=1, capacity=2, session_factory=None)
    second = m.submit(123, user_id=1, capacity=2, session_factory=None)

    await asyncio.wait_for(m.wait(123), timeout=2)

    assert first == {"queued": False, "position": 0}
    assert second == {"queued": True, "position": 1}
    assert active["max"] == 1
    assert calls == [123, 123]


async def test_wait_unknown_run_returns_immediately():
    m = RunManager()
    await asyncio.wait_for(m.wait(999), timeout=1)


async def test_user_sem_rebuilds_on_capacity_change():
    """M2: 用户并发度配置变更后 user_sem 应重建生效(原实现首建即冻结→改 max_llm_concurrency 须重启)。
    同容量仍复用同一信号量(保持单一并发上限)。"""
    m = RunManager()
    s1 = m.user_sem(1, 4)
    assert m.user_sem(1, 4) is s1          # 同容量复用同一信号量
    s2 = m.user_sem(1, 8)
    assert s2 is not s1                      # 容量变了 → 重建
    assert s2._value == 8                    # 新容量生效
    assert m.user_sem(1, 8) is s2            # 再次同容量复用
