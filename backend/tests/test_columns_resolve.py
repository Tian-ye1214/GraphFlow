import json

from app.engine.columns import resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Dataset


async def test_resolve_dataset_cols_skips_foreign(client, session_factory):
    async with session_factory() as s:
        mine = Dataset(user_id=1, name="mine", columns_json=json.dumps(["q"]))
        theirs = Dataset(user_id=2, name="theirs", columns_json=json.dumps(["secret"]))
        s.add_all([mine, theirs])
        await s.commit()
        g = parse_graph({"nodes": [
            {"id": "a", "type": "input", "config": {"dataset_ids": [mine.id]}},
            {"id": "b", "type": "input", "config": {"dataset_ids": [theirs.id]}}], "edges": []})
        cols = await resolve_dataset_cols(s, g, user_id=1)
        mine_id = mine.id
    assert cols == {mine_id: ["q"]}  # 他人数据集被跳过，列名不泄露
