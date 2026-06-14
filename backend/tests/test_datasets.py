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
