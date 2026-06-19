from pathlib import Path

from app.models import Dataset

JSONL = '{"q": "你好"}\n{"q": "第二"}\n{"q": "第三"}\n'.encode("utf-8")


async def upload(client, *files):
    payload = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return await client.post("/api/datasets/upload", files=payload)


async def test_upload_single(auth_client):
    r = await upload(auth_client, ("种子.jsonl", JSONL))
    assert r.status_code == 200
    ds = r.json()[0]
    assert ds["name"] == "种子"
    assert ds["row_count"] == 3
    assert ds["columns"] == ["q"]


async def test_upload_multiple_files(auth_client):
    r = await upload(auth_client, ("a.jsonl", JSONL), ("b.csv", "q\nx\n".encode()))
    assert [d["row_count"] for d in r.json()] == [3, 1]


async def test_upload_bad_file_422(auth_client):
    r = await upload(auth_client, ("bad.txt", b"hello"))
    assert r.status_code == 422
    assert "bad.txt" in r.json()["detail"]


async def test_upload_multi_sheet_excel_one_dataset_per_sheet(auth_client):
    """多 sheet Excel：每个非空 sheet 各成一个数据集（名=stem-sheet名），空 sheet 跳过。"""
    import io
    import pandas as pd
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        pd.DataFrame([{"q": "甲"}, {"q": "乙"}]).to_excel(w, sheet_name="表一", index=False)
        pd.DataFrame([{"k": "v"}]).to_excel(w, sheet_name="表二", index=False)
        pd.DataFrame().to_excel(w, sheet_name="空", index=False)
    r = await upload(auth_client, ("多表.xlsx", buf.getvalue()))
    assert r.status_code == 200
    by = {d["name"]: d for d in r.json()}
    assert set(by) == {"多表-表一", "多表-表二"}
    assert by["多表-表一"]["row_count"] == 2 and by["多表-表二"]["row_count"] == 1


async def test_upload_non_object_records_422(auth_client):
    """非对象记录（标量/null/数组/混入裸值）应 422，而非 500 或静默生成损坏数据集。"""
    for name, content in [("s.json", b"42"), ("n.json", b"null"), ("arr.json", b"[1,2,3]"),
                          ("str.json", b'"hi"'), ("bare.jsonl", b'{"q":1}\n99\n')]:
        r = await upload(auth_client, (name, content))
        assert r.status_code == 422, f"{name} 应 422，实得 {r.status_code}"


async def test_rows_pagination(auth_client):
    ds = (await upload(auth_client, ("a.jsonl", JSONL))).json()[0]
    r = (await auth_client.get(f"/api/datasets/{ds['id']}/rows?page=2&page_size=2")).json()
    assert r["total"] == 3
    assert r["rows"] == [{"q": "第三"}]


async def test_delete(auth_client):
    ds = (await upload(auth_client, ("a.jsonl", JSONL))).json()[0]
    assert (await auth_client.delete(f"/api/datasets/{ds['id']}")).status_code == 200
    assert (await auth_client.get("/api/datasets")).json() == []


async def test_user_isolation(auth_client):
    ds = (await upload(auth_client, ("a.jsonl", JSONL))).json()[0]
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/datasets")).json() == []
    assert (await auth_client.get(f"/api/datasets/{ds['id']}/rows")).status_code == 404
    assert (await auth_client.delete(f"/api/datasets/{ds['id']}")).status_code == 404


async def test_traversal_filename_sanitized(auth_client, session_factory):
    r = await upload(auth_client, ("../../evil.jsonl", JSONL))
    assert r.status_code == 200
    ds_id = r.json()[0]["id"]
    async with session_factory() as s:
        ds = await s.get(Dataset, ds_id)
    p = Path(ds.file_path)
    assert p.exists()
    assert p.parent.name == str(ds.user_id) and p.parent.parent.name == "uploads"


async def test_export_dataset_jsonl(auth_client):
    import json as _json
    ds = (await upload(auth_client, ("导出集.jsonl", JSONL))).json()[0]
    r = await auth_client.get(f"/api/datasets/{ds['id']}/export", params={"format": "jsonl"})
    assert r.status_code == 200
    lines = [l for l in r.text.splitlines() if l]
    assert len(lines) == 3 and _json.loads(lines[0])["q"] == "你好"


async def test_export_dataset_csv(auth_client):
    ds = (await upload(auth_client, ("c.jsonl", JSONL))).json()[0]
    r = await auth_client.get(f"/api/datasets/{ds['id']}/export", params={"format": "csv"})
    assert r.status_code == 200 and "q" in r.text and "你好" in r.text


async def test_export_dataset_rejects_foreign(auth_client, session_factory):
    from sqlalchemy import select
    from app.models import User
    async with session_factory() as s:
        stranger = User(username="ds_stranger", display_name="x")
        s.add(stranger); await s.commit()
        ds = Dataset(user_id=stranger.id, name="他人集", row_count=0, columns_json="[]")
        s.add(ds); await s.commit(); did = ds.id
    assert (await auth_client.get(f"/api/datasets/{did}/export")).status_code == 404


async def test_create_dataset_upsert_by_run_id(session_factory):
    """save_as_dataset 落库按 (run_id, name) 幂等：同 run 重算 output 覆盖更新而非产生重复数据集。"""
    from sqlalchemy import select
    from app.models import User
    from app.routers.datasets import create_dataset
    async with session_factory() as s:
        u = User(username="dsup", display_name="x"); s.add(u); await s.flush(); uid = u.id
        ds1 = await create_dataset(s, uid, "结果集", [{"a": 1}, {"a": 2}], source="run", run_id=42)
        first_id = ds1.id
    async with session_factory() as s:   # 同 run 同名重算 → 覆盖更新，不新建
        ds2 = await create_dataset(s, uid, "结果集", [{"a": 1}, {"a": 2}, {"a": 3}], source="run", run_id=42)
        assert ds2.id == first_id and ds2.row_count == 3
    async with session_factory() as s:
        same = (await s.execute(select(Dataset).where(Dataset.name == "结果集", Dataset.run_id == 42))).scalars().all()
        assert len(same) == 1
        await create_dataset(s, uid, "结果集", [{"a": 9}], source="run", run_id=99)   # 不同 run 各自独立
        await create_dataset(s, uid, "结果集", [{"a": 1}], run_id=None)               # 上传(run_id=None)永不 upsert
        await create_dataset(s, uid, "结果集", [{"a": 1}], run_id=None)
    async with session_factory() as s:
        total = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalars().all()
        assert len(total) == 4   # run42(1) + run99(1) + 两次上传(2)


async def test_upload_nan_json_rows_renderable(auth_client):
    """上传含 NaN/Infinity 的 JSON：归一为 null，上传 200 且 /rows 不再永久 500。"""
    r = await upload(auth_client, ("nan.json", b'[{"x": NaN, "y": Infinity, "ok": "v"}]'))
    assert r.status_code == 200
    ds = r.json()[0]
    rr = await auth_client.get(f"/api/datasets/{ds['id']}/rows")
    assert rr.status_code == 200
    assert rr.json()["rows"] == [{"x": None, "y": None, "ok": "v"}]


async def test_upload_deep_nested_json_422(auth_client):
    """深层嵌套 JSON 上传 → 422，不逃逸 500（RecursionError）。"""
    deep = ('[{"x":' + '[' * 6000 + '1' + ']' * 6000 + '}]').encode()
    r = await upload(auth_client, ("deep.json", deep))
    assert r.status_code == 422


async def test_export_control_char_name_no_500(auth_client, session_factory):
    r"""数据集名含控制字符(\r\n\t，可经构造多 sheet Excel 注入)时导出不应 500（清洗文件名）。"""
    from sqlalchemy import select
    from app.models import DatasetRow, User
    async with session_factory() as s:
        uid = (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()
        ds = Dataset(user_id=uid, name="book-bad\r\n\tx", row_count=1, columns_json='["q"]')
        s.add(ds); await s.commit(); did = ds.id
        s.add(DatasetRow(dataset_id=did, idx=0, data_json='{"q": "v"}')); await s.commit()
    r = await auth_client.get(f"/api/datasets/{did}/export", params={"format": "jsonl"})
    assert r.status_code == 200
