import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Dataset, RunRow
from app.services.dataset_store import _write_shards, dump_manifest, visible_total

ARTIFACT_SHARD_SIZE = 100_000

_ILLEGAL_SEGMENT = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_segment(value: str) -> str:
    cleaned = _ILLEGAL_SEGMENT.sub("_", value).strip(" .")[:120].strip(" .")
    return cleaned or "node"


class ArtifactWriter:
    def __init__(
        self,
        root: Path,
        *,
        run_id: int,
        node_id: str,
        columns: list[str],
        shard_size: int = ARTIFACT_SHARD_SIZE,
    ):
        self.root = Path(root)
        self.run_id = run_id
        self.node_id = node_id
        self.columns = list(columns)
        self.shard_size = shard_size
        self.dir = self.root / "runs" / str(run_id) / _safe_segment(node_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._current = None
        self._shard_rows = 0
        self._shard_no = 0
        self._row_count = 0
        self._shards: list[dict] = []
        self._closed_manifest: dict | None = None

    def append(self, file_row: int, rows: list[dict]) -> str:
        refs: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("artifact rows must be objects")
            if self._current is None or self._shard_rows >= self.shard_size:
                self._open_shard(file_row)
            for key in row:
                if key not in self.columns:
                    self.columns.append(key)
            rel = self._shards[-1]["path"]
            offset = self._shard_rows
            self._current.write(
                json.dumps({"file_row": file_row, "data": row}, ensure_ascii=False,
                           allow_nan=False) + "\n")
            self._shard_rows += 1
            self._row_count += 1
            self._shards[-1]["row_count"] = self._shard_rows
            self._shards[-1]["columns"] = list(self.columns)
            if refs and refs[-1]["path"] == rel and refs[-1]["offset"] + refs[-1]["count"] == offset:
                refs[-1]["count"] += 1
            else:
                refs.append({"path": rel, "offset": offset, "count": 1})
        return json.dumps(refs, separators=(",", ":"))

    def close(self) -> dict:
        if self._closed_manifest is not None:
            return self._closed_manifest
        if self._current is not None:
            self._current.close()
            self._current = None
        manifest = {
            "kind": "run_artifact",
            "run_id": self.run_id,
            "node_id": self.node_id,
            "columns": self.columns,
            "row_count": self._row_count,
            "root": str(self.root),
            "shards": self._shards,
        }
        manifest_path = self.dir / "artifact.json"
        manifest["manifest_path"] = manifest_path.relative_to(self.root).as_posix()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self._closed_manifest = manifest
        return manifest

    def _open_shard(self, file_row: int) -> None:
        if self._current is not None:
            self._current.close()
        self._shard_no += 1
        self._shard_rows = 0
        path = self.dir / f"part-{self._shard_no:06d}.jsonl"
        self._current = path.open("w", encoding="utf-8", newline="\n")
        self._shards.append({
            "path": path.relative_to(self.root).as_posix(),
            "start_file_row": file_row,
            "row_count": 0,
            "columns": list(self.columns),
        })


def load_artifact(path: Path) -> dict:
    manifest_path = Path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("root", str(manifest_path.parent.parent.parent.parent))
    return manifest


def iter_artifact_rows(manifest: dict, *, start_file_row: int | None = None,
                       end_file_row: int | None = None):
    root = Path(manifest.get("root", ""))
    for shard in manifest.get("shards", []):
        path = Path(shard["path"])
        if not path.is_absolute():
            path = root / path
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                item = json.loads(line)
                file_row = int(item.get("file_row", 0))
                if start_file_row is not None and file_row < start_file_row:
                    continue
                if end_file_row is not None and file_row > end_file_row:
                    continue
                yield file_row, item.get("data", {})


def iter_output_ref_rows(output_ref: str, data_dir: Path):
    if not output_ref:
        return
    refs = json.loads(output_ref)
    for ref in refs:
        path = Path(ref["path"])
        if not path.is_absolute():
            path = Path(data_dir) / path
        offset = int(ref.get("offset", 0))
        count = int(ref.get("count", 0))
        with path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh):
                if line_no < offset:
                    continue
                if line_no >= offset + count:
                    break
                item = json.loads(line)
                yield item.get("data", {})


def count_output_ref_rows(output_ref: str) -> int:
    if not output_ref:
        return 0
    return sum(int(ref.get("count", 0)) for ref in json.loads(output_ref))


def read_output_ref_rows(output_ref: str, data_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for row in iter_output_ref_rows(output_ref, data_dir):
        rows.append(row)
    return rows


def rows_for_rec(rec, data_dir: Path):
    """读出一条 RunRow 的逻辑行（内联 data_json 或溢出 artifact）——runner/runs 单点共用。"""
    if rec.output_ref:
        yield from iter_output_ref_rows(rec.output_ref, data_dir)
    else:
        yield from json.loads(rec.data_json or "[]")


async def iter_node_done_rows(session_factory, run_id: int, node_id: str, *,
                              data_dir: Path, batch_size: int = 500):
    """按 row_idx keyset 分页流式读某节点全部 done 行（每批一独立 session，不跨 yield 持连接）。
    runner._node_output_iter 与 runs._iter_done_rows 共用此单点。"""
    last_idx = -1
    while True:
        async with session_factory() as s:
            recs = (await s.execute(select(RunRow).where(
                RunRow.run_id == run_id,
                RunRow.node_id == node_id,
                RunRow.status == "done",
                RunRow.row_idx > last_idx,
            ).order_by(RunRow.row_idx).limit(batch_size))).scalars().all()
        if not recs:
            return
        for rec in recs:
            last_idx = rec.row_idx
            for row in rows_for_rec(rec, data_dir):
                yield row


async def register_artifact_as_dataset(
    session: AsyncSession,
    *,
    user_id: int,
    name: str,
    source_artifact: dict | Path,
    data_dir: Path,
    run_id: int | None = None,
    node_id: str | None = None,
) -> Dataset:
    artifact = load_artifact(source_artifact) if isinstance(source_artifact, Path) else source_artifact
    ds = None
    if run_id is not None and node_id is not None:
        ds = (await session.execute(select(Dataset).where(
            Dataset.user_id == user_id,
            Dataset.run_id == run_id,
            Dataset.node_id == node_id,
        ))).scalars().first()
    if ds is None:
        ds = Dataset(
            user_id=user_id,
            name=name,
            source="run",
            row_count=0,
            columns_json=json.dumps(artifact.get("columns", []), ensure_ascii=False),
            status="importing",
            imported_rows=0,
            original_format="jsonl",
            header_row=None,
            data_start_row=1,
            total_rows_including_header=0,
            run_id=run_id,
            node_id=node_id,
        )
        session.add(ds)
        await session.flush()
    else:
        ds.name = name
        ds.source = "run"
        ds.status = "importing"
        ds.columns_json = json.dumps(artifact.get("columns", []), ensure_ascii=False)
        ds.original_format = "jsonl"
        ds.header_row = None
        ds.data_start_row = 1
        ds.manifest_json = "{}"

    rows = (row for _, row in iter_artifact_rows(artifact))
    manifest, columns, row_count = _write_shards(
        rows,
        data_dir=data_dir,
        user_id=user_id,
        dataset_id=ds.id,
        version=ds.version,
        shard_size=ARTIFACT_SHARD_SIZE,
        columns=artifact.get("columns", []),
        header_row=None,
        data_start_row=1,
    )
    manifest["original_format"] = "jsonl"
    manifest["version_of_dataset_id"] = ds.version_of_dataset_id
    ds.manifest_json = dump_manifest(manifest)
    ds.columns_json = json.dumps(columns, ensure_ascii=False)
    ds.row_count = row_count
    ds.imported_rows = row_count
    ds.total_rows_including_header = visible_total(row_count, None)
    ds.status = "ready"
    await session.commit()
    return ds
