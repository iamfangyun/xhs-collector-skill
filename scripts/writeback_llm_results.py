#!/usr/bin/env python
# -*- coding: utf-8 -*-
# writeback_llm_results.py
# 把 llm_results.json 里的 LLM 推测结果回填到飞书笔记表「推测带货品类」字段
import json
import subprocess
import sys
import time
from pathlib import Path

LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_NOTE = "tblsIghwrc2TqemX"
PACING_API_SEC = 1.5

RESULTS_FILE = Path(r"C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09\xhs_llm_infer\llm_results.json")


def run_lark(args, timeout=120):
    cmd = [LARK] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        try:
            return json.loads(r.stdout) if r.stdout else {"_stderr": r.stderr}
        except json.JSONDecodeError:
            return {"_raw": r.stdout[:300], "_stderr": r.stderr[:300]}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}


def main():
    results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    print(f"=== 回填 LLM 推测结果 ({len(results)} 条) ===")

    BATCH = 20
    success = 0
    fail = 0
    for start in range(0, len(results), BATCH):
        batch = results[start:start + BATCH]
        payload = {"update_records": {r["record_id"]: {"推测带货品类": r["product"]} for r in batch}}
        time.sleep(PACING_API_SEC)
        r = run_lark([
            "base", "+record-batch-update",
            "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
            "--json", json.dumps(payload, ensure_ascii=False),
            "--as", "user",
        ], timeout=120)
        if r.get("ok"):
            success += len(batch)
            print(f"  batch {start // BATCH + 1}: OK ({len(batch)} 条)")
        else:
            fail += len(batch)
            print(f"  batch {start // BATCH + 1}: FAIL - {json.dumps(r, ensure_ascii=False)[:200]}")

    print()
    print(f"=== 完成 ===")
    print(f"  成功: {success}")
    print(f"  失败: {fail}")
    print(f"飞书: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")


if __name__ == "__main__":
    main()
