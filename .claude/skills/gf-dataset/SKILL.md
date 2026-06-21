---
name: gf-dataset
description: Use when 用 gf 命令管理 GraphFlow 数据集——列出/上传/下载/预览前几行/删除数据集；上传支持 jsonl/json/csv/xlsx/xls 多文件（Excel 多 sheet 拆多个数据集）
---

# gf-dataset —— 数据集

前置：先 `gf login`。数据集是用户级资源，与当前工作流无关。

| 命令 | 说明 |
|---|---|
| `gf data ls` | ID、名、行数、列名、来源(upload/run)、原始文件名、创建时间 |
| `gf data up <文件> [文件 …]` | 支持 jsonl/json/csv/xlsx/xls，多文件一次传。数据集名 = 文件名去扩展名；xlsx/xls 单个非空 sheet → 名=文件名去扩展名，多个非空 sheet → 每个一个数据集、名为「文件名去扩展名-sheet名」（空 sheet 跳过） |
| `gf data download <名\|ID> [-o 文件] [--format jsonl\|csv\|xlsx]` | 整集下载，默认 jsonl。缺省文件名取自你输入的指代 `<ref>`（按 ID 下载会得到数字名如 `5.jsonl`；想按数据集名命名用 `-o`） |
| `gf data head <名\|ID> [n]` | 预览前 n 行（默认 5），逐行 JSON |
| `gf data rm <名\|ID>` | 删除（连文件一起删） |

示例：

```powershell
gf data up seed.jsonl extra.csv          # 一次传多个，生成数据集「seed」「extra」
gf data head seed 10
gf data download seed --format xlsx -o out.xlsx
```

⚠️ **文件编码**：jsonl/json/csv 带 BOM 也能正常上传（后端 `utf-8-sig` 自动剥除 BOM，utf-8 解码失败再回退 GBK），无需特殊处理 BOM。

⚠️ 资源指代：`<名|ID>` 纯数字按 ID，否则按名精确匹配，重名报错列候选 ID（见 gf-cli 跨域坑）。
