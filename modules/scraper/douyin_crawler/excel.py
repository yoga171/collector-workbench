from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


COLUMNS = [
    "keyword",
    "product_id",
    "title",
    "original_price",
    "sale_price",
    "pay_count",
    "image_url",
    "shop_name",
    "source_url",
    "captured_at",
]


def jsonl_to_excel(jsonl_path: Path, excel_path: Path) -> int:
    rows = []
    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))

    deduped = {}
    for row in rows:
        key = row.get("row_id") or row.get("product_id") or row.get("title")
        deduped[key] = row

    data = list(deduped.values())
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(data)
    for column in COLUMNS:
        if column not in df.columns:
            df[column] = ""
    df = df[COLUMNS]
    df.to_excel(excel_path, index=False)
    return len(df)


def rows_to_excel(rows: list[dict], excel_path: Path) -> int:
    deduped = {}
    for row in rows:
        key = row.get("row_id") or row.get("product_id") or row.get("title")
        if key:
            deduped[key] = row

    data = list(deduped.values())
    excel_path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(data)
    for column in COLUMNS:
        if column not in df.columns:
            df[column] = ""
    for column in df.columns:
        if column not in COLUMNS:
            COLUMNS.append(column)
    df = df[COLUMNS]
    df.to_excel(excel_path, index=False)
    return len(df)
