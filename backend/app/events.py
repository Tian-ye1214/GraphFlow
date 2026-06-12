import asyncio
import json

subscribers: dict[int, set[asyncio.Queue]] = {}


def publish(user_id: int, entity: str, entity_id: int, **extra) -> None:
    payload = json.dumps({"entity": entity, "id": entity_id, **extra}, ensure_ascii=False)
    for q in subscribers.get(user_id, ()):
        q.put_nowait(payload)


def subscribe(user_id: int) -> asyncio.Queue:
    q = asyncio.Queue()
    subscribers.setdefault(user_id, set()).add(q)
    return q


def unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    subs = subscribers.get(user_id)
    if subs is not None:
        subs.discard(q)
        if not subs:
            del subscribers[user_id]
