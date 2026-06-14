"""BUG C 最小复现：多父节点(此处=一个 input 节点选两个 schema 不同的数据集)的列血缘虚报。
列血缘(columns.py)对多上游用 union，暗示「每行都有这些列」；
但执行(runner._node_inputs / _barrier_output input)是把各上游的行【拼接】，每行只带自己那支的列。
用法：PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/repro_bugC.py
"""
import asyncio
import io
import json
import tempfile
from pathlib import Path

from app.config import settings


async def main():
    settings.data_dir = Path(tempfile.mkdtemp(prefix="gf_bugC_"))
    from app import db, events
    events.subscribers.clear()
    await db.init_db()
    import httpx
    from app.engine.manager import manager
    from app.engine import nodes
    from app.main import create_app
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test")
    await c.post("/api/auth/login", json={"username": "u"})

    def up(name, rows):
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode()
        return [("files", (name, io.BytesIO(body), "application/octet-stream"))]

    # 两个 schema 不同的数据集：ds1 有列 [id,q]，ds2 有列 [id,text]
    ds1 = (await c.post("/api/datasets/upload",
           files=up("ds1.jsonl", [{"id": 1, "q": "问一"}, {"id": 2, "q": "问二"}]))).json()[0]
    ds2 = (await c.post("/api/datasets/upload",
           files=up("ds2.jsonl", [{"id": 3, "text": "文三"}, {"id": 4, "text": "文四"}]))).json()[0]
    print(f"ds1 列={ds1['columns']}  ds2 列={ds2['columns']}")

    wf = (await c.post("/api/workflows", json={"name": "多数据集"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds1["id"], ds2["id"]]}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [{"source": "in", "target": "out", "kind": "normal"}]}
    await c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    cols = (await c.get(f"/api/workflows/{wf['id']}/columns")).json()
    print(f"\n[列血缘] GET /columns 报 in.output = {cols['in']['output']}")
    print("           → 面板告诉用户：这里的每行都有 id / q / text 三列")

    rid = (await c.post("/api/runs", json={"workflow_id": wf['id']})).json()["id"]
    await manager.wait(rid)
    rows = (await c.get(f"/api/runs/{rid}/rows?node_id=out&page_size=100")).json()["rows"]
    print(f"\n[实际执行] out 共 {len(rows)} 行（两数据集拼接，非按 id join）：")
    for r in rows:
        print(f"           {r}   ← 缺列: {sorted(set(['id','q','text']) - set(r))}")

    both = [r for r in rows if "q" in r and "text" in r]
    print(f"\n[矛盾] 同时拥有 q 和 text 的行 = {len(both)} 行（血缘却宣称三列齐备）")

    # 下游后果：用户照面板写模板 "{{q}}/{{text}}"，半数行渲染出空洞
    print("\n[下游后果] 若依面板在合并后的节点写模板  '{{q}} | {{text}}' ：")
    for r in rows:
        print(f"           行 {r.get('id')} 渲染 = {nodes.render_template('{{q}} | {{text}}', r)!r}")

    await c.aclose()
    await db.engine.dispose()


asyncio.run(main())
