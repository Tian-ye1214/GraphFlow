---
name: gf-dataset
description: Use when 用 gf 命令管理 GraphFlow 数据集——列出/上传/下载/预览前几行/删除数据集；上传支持 jsonl/json/csv/xlsx 多文件，遇到上传报「Unexpected UTF-8 BOM」时
---

# gf-dataset —— 数据集

前置：先 `gf login`。数据集是用户级资源，与当前工作流无关。

| 命令 | 说明 |
|---|---|
| `gf data ls` | ID、名、行数、列名 |
| `gf data up <文件> [文件 …]` | 支持 jsonl/json/csv/xlsx，多文件一次传；数据集名 = 文件名去扩展名（xlsx 多 sheet 每表一个数据集） |
| `gf data download <名\|ID> [-o 文件] [--format jsonl\|csv\|xlsx]` | 整集下载，默认 jsonl，缺省文件名 `<ref>.<格式>` |
| `gf data head <名\|ID> [n]` | 预览前 n 行（默认 5），逐行 JSON |
| `gf data rm <名\|ID>` | 删除（连文件一起删） |

示例：

```powershell
gf data up seed.jsonl extra.csv          # 一次传多个，生成数据集「seed」「extra」
gf data head seed 10
gf data download seed --format xlsx -o out.xlsx
```

⚠️ **上传 jsonl/csv 别带 BOM**：PowerShell `Out-File`/`WriteAllText` 默认带 BOM，上传报「Unexpected UTF-8 BOM」。写文件用 `[IO.File]::WriteAllText($p, $s, [Text.UTF8Encoding]::new($false))`。

⚠️ 资源指代：`<名|ID>` 纯数字按 ID，否则按名精确匹配，重名报错列候选 ID（见 gf-cli 跨域坑）。
