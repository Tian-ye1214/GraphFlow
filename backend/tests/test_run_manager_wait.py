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


async def test_wait_unknown_run_returns_immediately():
    m = RunManager()
    await asyncio.wait_for(m.wait(999), timeout=1)
