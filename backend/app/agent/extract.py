"""文档文本提取：pdf/docx/xlsx/txt/md/csv/json(l)/html → 纯文本。路径由调用方解析。"""
import re
from html.parser import HTMLParser
from pathlib import Path

_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "div", "footer",
               "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main", "p", "section", "tr"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._ignored_depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return "\n".join(c for c in self._chunks if c.strip())


def _clean(content: str) -> str:
    content = re.sub(r"[ \t]{2,}", " ", content)
    content = re.sub(r"^[ \t]+|[ \t]+$", "", content, flags=re.MULTILINE)
    content = re.sub(r"\n{2,}", "\n", content).strip("\n")
    return content.strip()


def _from_pdf(fp: Path) -> str:
    import fitz
    with fitz.open(fp) as pdf:
        return "\n".join(page.get_text() for page in pdf)


def _from_excel(fp: Path) -> str:
    import pandas as pd
    sheets = pd.read_excel(fp, sheet_name=None, dtype=object, keep_default_na=False)
    parts = []
    for sheet_name, df in sheets.items():
        header = f"[Sheet: {sheet_name}]"
        body = "\n\n".join(f"{col}:\n{df[col].to_string()}" for col in df.columns)
        parts.append(f"{header}\n{body}")
    return "\n\n".join(parts)


def _from_docx(fp: Path) -> str:
    import docx
    return "\n".join(p.text for p in docx.Document(str(fp)).paragraphs)


def _from_html(fp: Path) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(fp.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return parser.text()


def extract_text(file_path: Path) -> str:
    if not file_path.exists():
        return f"Error: 文件不存在: {file_path.name}"
    ext = file_path.suffix.lower()
    try:
        if ext == ".pdf":
            content = _from_pdf(file_path)
        elif ext in (".xlsx", ".xls"):
            content = _from_excel(file_path)
        elif ext == ".docx":
            content = _from_docx(file_path)
        elif ext in (".txt", ".md", ".markdown", ".csv", ".json", ".jsonl"):
            content = file_path.read_text(encoding="utf-8", errors="replace")
        elif ext in (".html", ".htm"):
            content = _from_html(file_path)
        else:
            return f"Error: 不支持的文件类型 '{ext}'"
    except Exception as e:
        return f"Error: 提取失败: {e}"
    content = _clean(content)
    return content if content else "文件为空"
