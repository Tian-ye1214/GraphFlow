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
    await upload(auth_client, ("a.jsonl", JSONL))
    await auth_client.post("/api/auth/login", json={"username": "other"})
    assert (await auth_client.get("/api/datasets")).json() == []
