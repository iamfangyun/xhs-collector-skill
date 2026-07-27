#!/usr/bin/env python
# -*- coding: utf-8 -*-
# backfill_product_category.py
#
# 扫描飞书笔记表所有记录, 对每条笔记的"标题+正文+话题标签"跑推测函数,
# 把结果回填到"推测带货品类"字段 (fldxr0LEVZ)。
#
# 用法: python backfill_product_category.py
import json
import subprocess
import sys
import time
from pathlib import Path

LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_NOTE = "tblsIghwrc2TqemX"
FIELD_ID_CATEGORY = "fldxr0LEVZ"  # 推测带货品类

PACING_API_SEC = 1.5

# 把 collect.py 同目录加入 path, 复用它的 infer_product_category
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from collect import infer_product_category  # noqa


def run_lark(args, timeout=60):
    cmd = [LARK] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        try:
            return json.loads(r.stdout) if r.stdout else {"_stderr": r.stderr}
        except json.JSONDecodeError:
            return {"_raw": r.stdout[:300], "_stderr": r.stderr[:300]}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}


def extract_text(val):
    """从 cell value 提取纯文本 (text 字段可能是 str 或 list of segments)。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for seg in val:
            if isinstance(seg, dict):
                parts.append(seg.get("text", seg.get("name", "")))
            elif isinstance(seg, str):
                parts.append(seg)
        return "".join(parts)
    return str(val)


def list_all_notes():
    """返回 [(record_id, title, desc, tags_str, current_category), ...]"""
    r = run_lark([
        "base", "+record-list",
        "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
        "--limit", "200",
        "--as", "user", "--format", "json",
    ])
    if not r.get("ok"):
        raise RuntimeError(f"record-list 失败: {json.dumps(r, ensure_ascii=False)[:300]}")

    data = r["data"]
    rids = data.get("record_id_list", [])
    rows = data.get("data", [])
    fields = data.get("fields", [])
    name_to_idx = {f: i for i, f in enumerate(fields)}

    out = []
    for i, rid in enumerate(rids):
        row = rows[i] if i < len(rows) else []

        def cell(fname):
            idx = name_to_idx.get(fname, -1)
            return extract_text(row[idx]) if 0 <= idx < len(row) else ""

        title = cell("标题")
        desc = cell("正文")
        tags = cell("话题标签")
        current = cell("推测带货品类")
        out.append((rid, title, desc, tags, current))
    return out


def update_category(record_id, category_value):
    """批量接口需要包装成 update_records 字典, 这里单条调用更直观。"""
    payload = {"update_records": {record_id: {"推测带货品类": category_value}}}
    r = run_lark([
        "base", "+record-batch-update",
        "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
        "--json", json.dumps(payload, ensure_ascii=False),
        "--as", "user",
    ])
    return r.get("ok", False)


def main():
    print("=== 笔记表「推测带货品类」回填 ===")
    print()

    # 1. 拉全部笔记
    print("[1/3] 拉取笔记表所有记录...")
    notes = list_all_notes()
    print(f"      共 {len(notes)} 条")

    skip_count = 0
    pending = []
    for rid, title, desc, tags, cur in notes:
        if cur and cur.strip() and cur.strip() != "未识别":
            # 已经有值就不重写, 避免覆盖之前的结果
            skip_count += 1
            continue
        pending.append((rid, title, desc, tags))

    print(f"      已有值跳过: {skip_count}")
    print(f"      待回填: {len(pending)}")

    if not pending:
        print("      没有需要回填的, 退出")
        return

    # 2. 跑推测, 先打印所有结果让用户审一眼
    print()
    print("[2/3] 推测结果预览:")
    print(f"{'':4s}{'record_id':22s}{'title':30s}{'category':20s}evidence")
    print("-" * 120)
    updates = []  # [(rid, category), ...]
    from collections import Counter
    cat_counter = Counter()
    for i, (rid, title, desc, tags) in enumerate(pending, 1):
        cat, ev = infer_product_category(title, desc, tags)
        updates.append((rid, cat))
        if cat != "未识别":
            for sub in cat.split(","):
                cat_counter[sub.strip()] += 1
        # 打印 (截断长字段)
        t = (title or "(无标题)")[:28]
        print(f"{i:>3d} {rid:22s}{t:30s}{cat:20s}{ev[:60]}")

    print()
    print(f"      命中: {sum(1 for _, c in updates if c != '未识别')}/{len(updates)}")
    print(f"      分布:")
    for cat, cnt in cat_counter.most_common():
        print(f"        - {cat}: {cnt}")
    unident = sum(1 for _, c in updates if c == "未识别")
    if unident:
        print(f"        - 未识别: {unident}")

    # 3. 批量写入
    print()
    print(f"[3/3] 批量回填 ({len(updates)} 条)...")

    # 用 batch-update 一次更新所有 (飞书支持一次更新多条, 比单条循环快很多)
    # 但 payload 太大会被拒, 分批每次 20 条
    BATCH_SIZE = 20
    success = 0
    fail = 0
    for batch_start in range(0, len(updates), BATCH_SIZE):
        batch = updates[batch_start:batch_start + BATCH_SIZE]
        payload = {"update_records": {rid: {"推测带货品类": cat} for rid, cat in batch}}
        time.sleep(PACING_API_SEC)
        r = run_lark([
            "base", "+record-batch-update",
            "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
            "--json", json.dumps(payload, ensure_ascii=False),
            "--as", "user",
        ], timeout=120)
        if r.get("ok"):
            success += len(batch)
            print(f"      batch {batch_start // BATCH_SIZE + 1}: OK ({len(batch)} 条)")
        else:
            fail += len(batch)
            print(f"      batch {batch_start // BATCH_SIZE + 1}: FAIL - {json.dumps(r, ensure_ascii=False)[:200]}")

    print()
    print(f"=== 完成 ===")
    print(f"  成功: {success}")
    print(f"  失败: {fail}")
    print(f"  跳过(已有值): {skip_count}")
    print(f"飞书: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")


if __name__ == "__main__":
    main()
