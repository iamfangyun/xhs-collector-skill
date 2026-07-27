#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs-collector · 每日定时调度脚本 (v2)

由 WorkBuddy automation 在每天凌晨 5:00 触发执行。
流程:
    1. 启动后随机等待 1~10 分钟（模拟人类不规律作息）
    2. 扫描飞书账号表，拿到��有账号的 profile_url（完整主页 URL，含 xsec_token）
    3. 计算目标日期（北京时间昨天）
    4. 在飞书日志表里建一条 running 记录
    5. 逐个账号调用 collect.py + sync_to_lark.py（参数: --profile-url）
    6. 账号之间随机 5~8 分钟间隔
    7. 任何账号失败 → 立即停止，更新日志表为 failed，记录原因
    8. 全部成功 → 更新日志表为 success

v2 关键变化:
- 不再依赖 redId 调用 collect.py（v2.0.0 MCP 不接受 redId）
- 改为从飞书账号表"主页链接"字段读取完整 URL（含 xsec_token）传给 collect.py
- 如果 URL 里的 token 已失效，collect.py 会报错退出，scheduler 记录失败原因提示用户更新

失败策略: 遇错即停（用户硬性要求）
"""
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============== 配置 ==============
LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_ACCOUNT = "tbl9YZx9XsG1RoDN"
TBL_LOG = "tbltPTsDTPByT4oz"

SKILL_DIR = Path(r"C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts")
OUTPUT_ROOT = Path(r"C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09\xhs_scheduler_output")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

PACING_API_SEC = 1.5                # 飞书 API 间隔
STARTUP_JITTER_MIN = 1              # 启动随机等待最小分钟
STARTUP_JITTER_MAX = 10             # 启动随机等待最大分钟
ACCOUNT_GAP_MIN_MIN = 5             # 账号间隔最小分钟
ACCOUNT_GAP_MAX_MIN = 8             # 账号间隔最大分钟
BEIJING_TZ = timezone(timedelta(hours=8))


def run_lark(args, timeout=30):
    cmd = [LARK] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        try:
            return json.loads(r.stdout) if r.stdout else {"_stderr": r.stderr}
        except json.JSONDecodeError:
            return {"_raw": r.stdout[:500], "_stderr": r.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}


def get_beijing_yesterday():
    now_bj = datetime.now(BEIJING_TZ)
    yesterday = now_bj - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def _extract_text(val):
    """从 cell value 提取字符串。主页链接字段可能是纯 URL 字符串，也可能是 markdown。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for seg in val:
            if isinstance(seg, dict):
                # markdown 链接: {type: url, text: "...", link: "..."}
                if seg.get("type") == "url" and seg.get("link"):
                    return seg["link"]  # 优先返回纯链接
                parts.append(seg.get("text", seg.get("name", "")))
            else:
                parts.append(str(seg))
        return "".join(parts)
    return str(val)


def list_accounts():
    """扫描飞书账号表，返回 [{redId, nickname, user_id, profile_url}, ...]"""
    r = run_lark([
        "base", "+record-list",
        "--base-token", BASE_TOKEN, "--table-id", TBL_ACCOUNT,
        "--limit", "100",
        "--as", "user", "--format", "json",
    ])
    if not r.get("ok"):
        raise RuntimeError(f"扫描账号表失败: {json.dumps(r, ensure_ascii=False)[:300]}")

    data = r["data"]
    record_ids = data.get("record_id_list", [])
    rows_data = data.get("data", [])
    fields_meta = data.get("fields", [])

    name_to_idx = {f: i for i, f in enumerate(fields_meta)}

    accounts = []
    for i, rid in enumerate(record_ids):
        row = rows_data[i] if i < len(rows_data) else []

        def get_cell(fname):
            idx = name_to_idx.get(fname, -1)
            if idx < 0 or idx >= len(row):
                return ""
            return _extract_text(row[idx])

        red_id = get_cell("redId").strip()
        nickname = get_cell("昵称").strip()
        profile_url = get_cell("主页链接").strip()
        user_id = get_cell("user_id").strip()

        # 主页链接必须带 xsec_token 才能采集
        if "xsec_token" not in profile_url:
            print(f"  ⚠️ 跳过 {nickname} ({red_id}): 主页链接里没有 xsec_token")
            print(f"     请到小红书网页端打开该账号主页,复制浏览器地址栏完整 URL 更新到飞书表")
            continue

        if red_id:
            accounts.append({
                "redId": red_id, "nickname": nickname,
                "user_id": user_id, "profile_url": profile_url,
                "record_id": rid,
            })
    return accounts


def create_log_record(target_date, account_count, jitter_sec):
    now_str = time.strftime("%Y-%m-%d %H:%M")
    fields = {
        "任务开始时间": now_str,
        "目标日期": target_date,
        "待采集账号数": account_count,
        "随机等待秒数": int(jitter_sec),
        "任务状态": "running",
    }
    time.sleep(PACING_API_SEC)
    r = run_lark([
        "base", "+record-upsert",
        "--base-token", BASE_TOKEN, "--table-id", TBL_LOG,
        "--json", json.dumps(fields, ensure_ascii=False),
        "--as", "user",
    ])
    rec = r.get("data", {}).get("record", {})
    if "record_id_list" in rec:
        return rec["record_id_list"][0]
    if "record_id" in rec:
        return rec["record_id"]
    return None


def update_log_record(log_rid, **kwargs):
    if not log_rid:
        return
    fields = {k: v for k, v in kwargs.items() if v is not None}
    if not fields:
        return
    time.sleep(PACING_API_SEC)
    payload = {"update_records": {log_rid: fields}}
    run_lark([
        "base", "+record-batch-update",
        "--base-token", BASE_TOKEN, "--table-id", TBL_LOG,
        "--json", json.dumps(payload, ensure_ascii=False),
        "--yes", "--as", "user",
    ])


def collect_account(profile_url, target_date):
    """对单个账号执行采集+同步，返回 stats dict 或 raise"""
    # 1. 采集（用 --profile-url 而非 --red-id）
    print(f"  [collect] python collect.py --profile-url <URL> --date {target_date}")
    r1 = subprocess.run([
        sys.executable, str(SKILL_DIR / "collect.py"),
        "--profile-url", profile_url,
        "--date", target_date,
        "--out", str(OUTPUT_ROOT),
    ], capture_output=True, text=True, encoding="utf-8", timeout=3600)
    if r1.returncode != 0:
        raise RuntimeError(f"collect.py 退出码 {r1.returncode}\nstdout: {r1.stdout[-800:]}\nstderr: {r1.stderr[-500:]}")

    # 找到输出文件（collect.py 输出路径是 <out>/<redId>_<nick>/<redId>_<nick>_<date>.json）
    json_files = list(OUTPUT_ROOT.rglob(f"*_{target_date}.json"))
    if not json_files:
        raise RuntimeError(f"collect.py 完成但找不到输出 JSON（匹配 *_{target_date}.json）")
    json_file = json_files[-1]  # 取最新的

    with open(json_file, "rb") as f:
        collected = json.loads(f.read().decode("utf-8"))
    notes = collected.get("matched_notes", [])
    note_count = len(notes)
    comment_count = sum(n.get("comments", {}).get("count", 0) for n in notes)
    image_count = sum(len(n.get("image_files", [])) for n in notes)

    # 2. 同步飞书
    print(f"  [sync] python sync_to_lark.py --input {json_file.name}")
    r2 = subprocess.run([
        sys.executable, str(SKILL_DIR / "sync_to_lark.py"),
        "--input", str(json_file),
        "--data-root", str(OUTPUT_ROOT),
    ], capture_output=True, text=True, encoding="utf-8", timeout=1800, cwd=str(OUTPUT_ROOT))
    if r2.returncode != 0:
        raise RuntimeError(f"sync_to_lark.py 退出码 {r2.returncode}\nstdout: {r2.stdout[-500:]}\nstderr: {r2.stderr[-500:]}")

    return {
        "note_count": note_count,
        "comment_count": comment_count,
        "image_count": image_count,
        "has_published": note_count > 0,
    }


def main():
    print(f"=== xhs-collector v2 每日定时任务启动 ===")
    print(f"当前北京时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: 随机启动延迟
    jitter_sec = random.randint(STARTUP_JITTER_MIN * 60, STARTUP_JITTER_MAX * 60)
    print(f"\n[1/5] 随机等待 {jitter_sec} 秒（{jitter_sec // 60} 分 {jitter_sec % 60} 秒）...")
    for remaining in range(jitter_sec, 0, -30):
        print(f"  剩余 {remaining} 秒", flush=True)
        time.sleep(min(30, remaining))

    # Step 2: 计算目标日期
    target_date = get_beijing_yesterday()
    print(f"\n[2/5] 目标日期（北京时间昨天）: {target_date}")

    # Step 3: 扫描账号表
    print(f"\n[3/5] 扫描飞书账号表...")
    accounts = list_accounts()
    print(f"  共 {len(accounts)} 个有效账号（主页链接含 xsec_token）")
    for a in accounts:
        print(f"    - {a['redId']} ({a['nickname']})")
    if not accounts:
        print("  无账号可采集，任务结束")
        return

    # Step 4: 日志表
    print(f"\n[4/5] 创建日志记录...")
    log_rid = create_log_record(target_date, len(accounts), jitter_sec)
    print(f"  log record_id: {log_rid}")

    # Step 5: 逐账号采集
    print(f"\n[5/5] 开始采集（账号间隔 {ACCOUNT_GAP_MIN_MIN}-{ACCOUNT_GAP_MAX_MIN} 分钟）...")
    success_count = 0
    fail_count = 0
    total_notes = 0
    total_comments = 0
    total_images = 0
    failed_accounts = []
    error_details = []

    for i, acc in enumerate(accounts, 1):
        print(f"\n--- [{i}/{len(accounts)}] {acc['redId']} ({acc['nickname']}) ---")

        if i > 1:
            gap = random.randint(ACCOUNT_GAP_MIN_MIN * 60, ACCOUNT_GAP_MAX_MIN * 60)
            print(f"  账号间隔等待 {gap} 秒（{gap // 60} 分）...")
            for remaining in range(gap, 0, -60):
                print(f"    剩余 {remaining} 秒", flush=True)
                time.sleep(min(60, remaining))

        try:
            stats = collect_account(acc["profile_url"], target_date)
            success_count += 1
            total_notes += stats["note_count"]
            total_comments += stats["comment_count"]
            total_images += stats["image_count"]
            if not stats["has_published"]:
                print(f"  ✅ 该账号昨日无笔记发布，静默跳过")
            else:
                print(f"  ✅ 采集 {stats['note_count']} 篇笔记 / {stats['comment_count']} 评论 / {stats['image_count']} 图片")
        except Exception as e:
            fail_count += 1
            err_msg = str(e)
            failed_accounts.append(f"{acc['redId']}:{acc['nickname']} - {err_msg[:200]}")
            error_details.append(f"=== {acc['redId']} ({acc['nickname']}) ===\n{err_msg}\n")
            print(f"  ❌ FAIL: {err_msg[:400]}")
            print(f"\n⚠️ 遇错即停策略：终止后续账号采集")
            break

    end_time_str = time.strftime("%Y-%m-%d %H:%M")
    if fail_count == 0:
        status = "success"
    elif success_count > 0:
        status = "partial"
    else:
        status = "failed"

    print(f"\n=== 任务完成 ===")
    print(f"  状态: {status}")
    print(f"  成功: {success_count}/{len(accounts)}")
    print(f"  失败: {fail_count}")
    print(f"  笔记总数: {total_notes}")
    print(f"  评论总数: {total_comments}")
    print(f"  图片总数: {total_images}")

    update_log_record(
        log_rid,
        **{
            "任务结束时间": end_time_str,
            "成功账号数": success_count,
            "失败账号数": fail_count,
            "总笔记数": total_notes,
            "总评论数": total_comments,
            "总图片数": total_images,
            "任务状态": status,
            "失败账号列表": "\n".join(failed_accounts) if failed_accounts else None,
            "错误详情": "\n".join(error_details) if error_details else None,
        }
    )

    if fail_count > 0:
        print(f"\n⚠️ 任务失败，请检查飞书日志表 record_id={log_rid}")
        print(f"飞书 URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")
        sys.exit(1)


if __name__ == "__main__":
    main()
