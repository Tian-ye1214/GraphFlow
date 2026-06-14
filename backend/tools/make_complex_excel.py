"""生成一个「足够刁钻」的测试 Excel，把历轮修过的数据边角全塞进去，保存为 tools/complex_test.xlsx。
100 行 × 26 命名列（含 1 个重复列名 → 27 物理列）。

覆盖：空值/缺失、重复值、重复列名、乱码(多脚本 unicode/emoji/零宽/方向控制)、超长值(近 Excel 上限)、
列名冲突(dup→dup.1)、类型篡改源(前导零/20位长ID/布尔字面量,均以文本写入)、None vs 字符串"None"(去重撞键)、
_qc 前缀用户列(须存活)、点号/中文/emoji/超长 列名、模板字面量({{...}} 不应二次展开)、纯空白值。

用法：PYTHONIOENCODING=utf-8 python tools/make_complex_excel.py
"""
import random
from pathlib import Path

import pandas as pd

random.seed(42)
N = 100
OUT = Path(__file__).resolve().parents[2] / "samples" / "complex_test.xlsx"

QUESTIONS = ["什么是勾股定理？", "水的沸点是多少？", "光速约为多少？", "氢的原子序数？",
             "牛顿第二定律是什么？", "DNA 的全称？", "圆周率前五位？", "地球半径约多少？"]
CATEGORIES = ["数学", "物理", "化学", "历史", "生物"]
# 乱码/多脚本样本（均为合法 unicode，避开 openpyxl 非法控制字符 \x00-\x1f）
MOJI = ["😀🎉中文ABC", "Привет мир", "مرحبا بالعالم", "日本語テキスト한국어",
        "zero​width", "combińing", "rtl‮منعكس", "﻿BOM在中间"]
LONG = "长" * 32000  # 近 Excel 单元格上限 32767，测超长值

rows = []
for i in range(N):
    rows.append({
        "id": f"{i + 1:03d}",                                  # 前导零文本：001..100
        "phone": "0" + str(13800000000 + (i % 30)),            # 前导零 + 重复值
        "big_id": str(12345678901234567890 + i),               # 20 位长整型（文本，防 float 丢精度）
        "is_active": "true" if i % 2 else "false",             # 布尔字面量（文本，不应变 bool）
        "question": QUESTIONS[i % len(QUESTIONS)],             # 大量重复（去重用）
        "answer": ("答案见 {{question}}" if i % 7 == 0 else f"答案{i}"),  # 含模板字面量
        "category": CATEGORIES[i % len(CATEGORIES)],
        "score": i % 101,                                       # 真数值
        "price": "" if i % 6 == 0 else f"{i * 1.5:.2f}",       # 文本数值 + 部分空
        "empty_all": None,                                     # 整列空
        "sparse": (f"有值{i}" if i % 3 == 0 else None),         # 部分缺失
        "long_text": (LONG if i in (10, 50) else f"短{i}"),     # 超长值（2 行）
        "mojibake": MOJI[i % len(MOJI)],                       # 乱码/多脚本
        "_qc_score": i % 100,                                   # _qc 前缀用户列（须存活）
        "_qc_note": f"备注{i}",                                  # _qc 前缀用户列
        "none_or_str": (None if i % 4 == 0 else ("None" if i % 4 == 1 else f"v{i % 10}")),  # None vs "None"
        "中文列名": f"值{i}",                                     # 中文列名
        "col.with.dots": f"dot{i}",                            # 点号列名（模板 {{col.with.dots}}）
        "emoji😀列": f"e{i}",                                    # emoji 列名
        ("超长列名" * 50): f"x{i}",                              # 200 字列名
        "tags": "a,b,c",                                       # 逗号分隔（concat 用）
        "lang": ["zh", "en", "ja"][i % 3],
        "whitespace": ("   " if i % 5 == 0 else f"w{i}"),      # 纯空白值
        "tmpl_literal": "见 {{question}}",                      # 模板字面量（不应二次展开）
        "num_clean": str(i + 1),                               # 干净整型文本（cast 用，无空）
        "dup": f"A{i}",                                         # 重复列名（下面注入第二个 dup）
        "dup__second": f"B{i}",                                # 写出前改名为 dup → 读回消歧成 dup/dup.1
    })

df = pd.DataFrame(rows)
df = df.rename(columns={"dup__second": "dup"})  # 制造重复列名 dup,dup

# 多 sheet：测 sheet 读取能力。各 sheet schema 不同（→ 每 sheet 一个数据集）。
df_cfg = pd.DataFrame([{"category": c, "weight": round((i + 1) * 0.1, 1), "enabled": "true" if i % 2 else "false"}
                       for i, c in enumerate(CATEGORIES)])                     # 不同 schema 的配置表
df_num = pd.DataFrame([{"seq": i, "ratio": i / 3, "label": f"L{i:02d}"} for i in range(10)])  # 真数值(保数值)
df_empty = pd.DataFrame()                                                       # 空 sheet（应被跳过）

OUT.parent.mkdir(parents=True, exist_ok=True)
with pd.ExcelWriter(OUT) as w:
    df.to_excel(w, sheet_name="主数据", index=False)
    df_cfg.to_excel(w, sheet_name="配置", index=False)
    df_num.to_excel(w, sheet_name="数值表", index=False)
    df_empty.to_excel(w, sheet_name="空表", index=False)
print(f"主数据 列数={df.shape[1]}（含重复 dup）行数={df.shape[0]}；另有 配置/数值表/空表 sheet")
print(f"已保存 → {OUT}  ({OUT.stat().st_size} 字节)")
