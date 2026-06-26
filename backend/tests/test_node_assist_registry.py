import asyncio

from app.agent.node_assist import NodeAssistRegistry


async def test_cancel_matching_user_cancels_task():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    assert reg.cancel("c1", 7) is True
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_cancel_wrong_user_does_not_cancel():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    assert reg.cancel("c1", 99) is False     # 跨租户：不取消
    assert not task.done()
    task.cancel()
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_cancel_unknown_callid_false():
    reg = NodeAssistRegistry()
    assert reg.cancel("nope", 1) is False


async def test_discard_removes_entry():
    reg = NodeAssistRegistry()
    task = asyncio.ensure_future(asyncio.Event().wait())
    reg.register("c1", 7, task)
    reg.discard("c1")
    assert reg.cancel("c1", 7) is False      # 已注销 → 找不到
    task.cancel()
    import contextlib
    with contextlib.suppress(asyncio.CancelledError):
        await task
