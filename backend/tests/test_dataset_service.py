"""TDD tests for dataset_service (delete_dataset + ingest_file)."""
import json
from pathlib import Path
from app.config import settings
from app.models import Dataset, DatasetRow, User
from app.services import dataset_service
from sqlalchemy import func, select


async def _seed_user(sf):
    async with sf() as s:
        u = User(username="du"); s.add(u); await s.commit(); return u.id


async def test_ingest_file_creates_dataset_from_jsonl(session_factory, tmp_path):
    sf = session_factory; uid = await _seed_user(sf)
    src = tmp_path / "seed.jsonl"
    src.write_text('{"q": "a"}\n{"q": "b"}\n', encoding="utf-8")
    async with sf() as s:
        created = await dataset_service.ingest_file(s, sf, uid, "seed.jsonl", src)
    assert len(created) == 1
    ds_id = created[0].id
    # 摄入后台异步：轮询到 ready
    import asyncio
    for _ in range(40):
        async with sf() as s:
            if (await s.get(Dataset, ds_id)).status == "ready":
                break
        await asyncio.sleep(0.1)
    async with sf() as s:
        assert (await s.get(Dataset, ds_id)).status == "ready"


async def test_delete_dataset_cascades_rows(session_factory):
    sf = session_factory; uid = await _seed_user(sf)
    async with sf() as s:
        ds = Dataset(user_id=uid, name="d", source="upload", row_count=1,
                     columns_json="[]", status="ready", file_path="")
        s.add(ds); await s.flush()
        s.add(DatasetRow(dataset_id=ds.id, idx=0, data_json="{}"))
        await s.commit(); ds_id = ds.id
    async with sf() as s:
        ds = await s.get(Dataset, ds_id)
        await dataset_service.delete_dataset(s, ds, settings.data_dir)
    async with sf() as s:
        assert await s.get(Dataset, ds_id) is None
        cnt = (await s.execute(select(func.count()).select_from(DatasetRow)
               .where(DatasetRow.dataset_id == ds_id))).scalar()
        assert cnt == 0
