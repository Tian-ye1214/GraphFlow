import sqlalchemy

from app.agent.dataset_tools import DatasetToolkit
from app.models import Dataset, User


async def _seed_user(sf, username="dt"):
    async with sf() as s:
        u = User(username=username)
        s.add(u)
        await s.commit()
        return u.id


async def test_upload_dataset_from_workdir(session_factory, tmp_path):
    sf = session_factory
    uid = await _seed_user(sf)
    (tmp_path / "seed.jsonl").write_text('{"q":"a"}\n{"q":"b"}\n', encoding="utf-8")
    msg = await DatasetToolkit(sf, uid, tmp_path).upload_dataset("seed.jsonl")
    assert "上传" in msg or "摄入" in msg
    async with sf() as s:
        ds = (await s.execute(sqlalchemy.select(Dataset))).scalars().first()
        assert ds is not None


async def test_upload_dataset_path_escape_blocked(session_factory, tmp_path):
    sf = session_factory
    uid = await _seed_user(sf)
    msg = await DatasetToolkit(sf, uid, tmp_path).upload_dataset("../../etc/passwd")
    assert "Security error" in msg or "Error" in msg


async def test_delete_dataset_requires_confirmation(session_factory, tmp_path):
    sf = session_factory
    uid = await _seed_user(sf)
    async with sf() as s:
        ds = Dataset(
            user_id=uid, name="d", source="upload", row_count=0,
            columns_json="[]", status="ready", file_path="",
        )
        s.add(ds)
        await s.commit()
        did = ds.id
    msg = await DatasetToolkit(sf, uid, tmp_path).delete_dataset(did)
    assert "确认" in msg
    async with sf() as s:
        assert await s.get(Dataset, did) is not None
