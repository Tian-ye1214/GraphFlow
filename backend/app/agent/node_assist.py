"""节点助手在途调用的可取消句柄登记表：call_id → (user_id, Task)。
按 user_id 隔离——他人即便拿到 call_id 也无法取消本人调用（跨租户隔离）。"""
import asyncio


class NodeAssistRegistry:
    def __init__(self):
        self._entries: dict[str, tuple[int, asyncio.Task]] = {}

    def register(self, call_id: str, user_id: int, task: asyncio.Task) -> None:
        self._entries[call_id] = (user_id, task)

    def discard(self, call_id: str) -> None:
        self._entries.pop(call_id, None)

    def cancel(self, call_id: str, user_id: int) -> bool:
        entry = self._entries.get(call_id)
        if entry and entry[0] == user_id and not entry[1].done():
            entry[1].cancel()
            return True
        return False


node_assist_registry = NodeAssistRegistry()
