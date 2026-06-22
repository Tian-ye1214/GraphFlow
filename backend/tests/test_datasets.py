from pathlib import Path

from sqlalchemy import select

from app.models import Dataset, User

JSONL = '{"q": "你好"}\n{"q": "第二"}\n{"q": "第三"}\n'.encode("utf-8")


async def upload(client, *files):
    payload = [("files", (name, content, "application/octet-stream")) for name, content in files]
    return await client.post("/api/datasets/upload", files=payload)


async def test_upload_overlong_filename_no_500(auth_client):
    """超长上传文件名不得令 write_bytes 抛 OSError(NTFS 255/MAX_PATH 260)逃逸成 500；
    _safe_filename 限长 + 写入处 OSError→422 兜底：写得下则 200，写不下则优雅 422，绝不 500。"""
    r = await upload(auth_client, ("n" * 300 + ".jsonl", JSONL))
    assert r.status_code in (200, 422)


async def test_export_overlong_dataset_name_no_500(auth_client, session_factory):
    """超长数据集名导出不得令 write_text 抛 OSError 逃逸成 500；_safe_filename 限长 + OSError→422 兜底。"""
    async with session_factory() as s:
        uid = (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()
        ds = Dataset(user_id=uid, name="x" * 300, source="run", row_count=0, columns_json="[]")
        s.add(ds)
        await s.commit()
        ds_id = ds.id
    e = await auth_client.get(f"/api/datasets/{ds_id}/export?format=jsonl")
    assert e.status_code in (200, 422)


async def test_upload_oserror_degrades_422(auth_client, monkeypatch):
    """确定性锁住 OSError→422 兜底：写盘抛 OSError(如路径超 MAX_PATH)须 422 优雅降级，绝不 500。"""
    def boom(self, *args, **kwargs):
        raise OSError("path too long")
    monkeypatch.setattr(Path, "open", boom)
    r = await upload(auth_client, ("ok.jsonl", JSONL))
    assert r.status_code == 422


async def test_export_oserror_degrades_422(auth_client, session_factory, monkeypatch):
    """确定性锁住 export OSError→422 兜底。"""
    async with session_factory() as s:
        uid = (await s.execute(select(User.id).where(User.username == "tester"))).scalar_one()
        ds = Dataset(user_id=uid, name="d", source="run", row_count=0, columns_json="[]")
        s.add(ds)
        await s.commit()
        ds_id = ds.id

    async def boom(*a, **k):
        raise OSError("path too long")
    monkeypatch.setattr("app.routers.datasets.write_csv_export", boom)
    e = await auth_client.get(f"/api/datasets/{ds_id}/export?format=csv")
    assert e.status_code == 422


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


async def test_create_dataset_upsert_by_run_node(session_factory):
    """save_as_dataset 落库按 (run_id, node_id, name) 幂等：同 run 同节点重算覆盖更新；
    不同节点同名各自独立(不互相覆盖丢数据)；上传(run_id=None)永不 upsert。"""
    from sqlalchemy import select
    from app.models import User
    from app.routers.datasets import create_dataset
    async with session_factory() as s:
        u = User(username="dsup", display_name="x"); s.add(u); await s.flush(); uid = u.id
        ds1 = await create_dataset(s, uid, "结果集", [{"a": 1}, {"a": 2}], source="run", run_id=42, node_id="out")
        first_id = ds1.id
    async with session_factory() as s:   # 同 run 同节点重算 → 覆盖更新，不新建
        ds2 = await create_dataset(s, uid, "结果集", [{"a": 1}, {"a": 2}, {"a": 3}], source="run", run_id=42, node_id="out")
        assert ds2.id == first_id and ds2.row_count == 3
    async with session_factory() as s:
        same = (await s.execute(select(Dataset).where(Dataset.run_id == 42, Dataset.name == "结果集"))).scalars().all()
        assert len(same) == 1
        # 同 run 不同 output 节点用了相同默认名 → 各自独立，绝不互相覆盖丢数据
        await create_dataset(s, uid, "结果集", [{"a": 9}], source="run", run_id=42, node_id="out2")
        await create_dataset(s, uid, "结果集", [{"a": 8}], source="run", run_id=99, node_id="out")   # 不同 run 独立
        await create_dataset(s, uid, "结果集", [{"a": 1}], run_id=None)   # 上传永不 upsert
        await create_dataset(s, uid, "结果集", [{"a": 1}], run_id=None)
    async with session_factory() as s:
        total = (await s.execute(select(Dataset).where(Dataset.name == "结果集"))).scalars().all()
        assert len(total) == 5   # run42/out(1)+run42/out2(1)+run99/out(1)+两次上传(2)


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
