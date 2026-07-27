#!/usr/bin/env python
# -*- coding: utf-8 -*-
# xhs-collector 每日定时调度脚本 (v2.1)
#
# 由 WorkBuddy automation 在每天凌晨 5:00 触发执行。
# 流程:
#     1. 启动后随机等待 1~10 分钟 (模拟人类不规律作息)
#     2. 扫描��书账号表, 拿到所有账号的 redId / profile_url
#     3. 计算目标日期 (北京时间昨天)
#     4. 在飞书日志表里建一条 running 记录
#     5. 逐个账号调用 collect.py + sync_to_lark.py
#        - profile_url 含 xsec_token: 直接用 --profile-url 模式
#        - profile_url 不含 xsec_token 或采集失败 (token 过期): 自动切到 --red-id 模式
#          通过 CDP+Edge 刷新 token, 并把新 URL 回写到飞书账号表
#     6. 账号之间随机 5~8 分钟间隔
#     7. 任何账号失败 (刷新也失败) → 立即停止, 更新日志表为 failed
#     8. 全部成功 → 更新日志表为 success
#
# v2.1 新增 (2026-07-27):
#   - 支持账号 URL 里没有 xsec_token 的场景: 自动用 --red-id + CDP 刷新
#   - 采集失败 (token 过期) 时自动 fallback 到 --red-id 重试
#   - 刷新成功后回写飞书账号表"主页链接"字段, 下次直接用
#
# v2.0 变化:
#   - 不再依赖 redId 调用 collect.py (v2.0.0 MCP 不接受 redId)
#   - 改为从飞书账号表"主页链接"字段读取完整 URL (含 xsec_token) 传给 collect.py
#
# 失败策略: 遇错即停 (用户硬性要求)
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

# token 过期错误识别关键词 (出现这些说明需要刷新 token)
TOKEN_EXPIRED_KEYWORDS = ["xsec_token", "token", "登录已过期", "verify", "权限"]


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
    """从 cell value 提取字符串。主页链接字段可能是纯 URL 字符串, 也可能是 markdown。"""
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
    """
    扫描飞书账号表, 返回 [{redId, nickname, user_id, profile_url, needs_refresh, record_id}, ...]

    needs_refresh=True 表示 profile_url 里没有 xsec_token, 需要走 CDP 自动刷新流程。
    """
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

        if not red_id:
            print(f"  [skip] 第 {i+1} 行没有 redId, 跳过")
            continue

        # 关键判断: URL 是否带 xsec_token
        needs_refresh = "xsec_token" not in profile_url
        if needs_refresh:
            print(f"  [warn] {nickname} ({red_id}): 主页链接里没有 xsec_token, 将使用 --red-id 自动刷新")

        accounts.append({
            "redId": red_id,
            "nickname": nickname,
            "user_id": user_id,
            "profile_url": profile_url,
            "needs_refresh": needs_refresh,
            "record_id": rid,
        })
    return accounts


def update_account_profile_url(record_id, new_profile_url):
    """刷新成功后把新 URL 回写到飞书账号表的"主页链接"字段。"""
    payload = {"update_records": {record_id: {"主页链接": {"type": "url", "link": new_profile_url}}}}
    time.sleep(PACING_API_SEC)
    run_lark([
        "base", "+record-batch-update",
        "--base-token", BASE_TOKEN, "--table-id", TBL_ACCOUNT,
        "--json", json.dumps(payload, ensure_ascii=False),
        "--yes", "--as", "user",
    ])


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


def _run_collect(profile_url, red_id, target_date):
    """
    调用 collect.py。
    - 如果 profile_url 非空且含 xsec_token: 用 --profile-url 模式
    - 否则: 用 --red-id 模式 (会自动调 refresh_token.py)
    返回最新输出 JSON 文件的 Path。
    """
    if profile_url and "xsec_token" in profile_url:
        cmd_mode = ["--profile-url", profile_url]
        print(f"  [collect] mode=profile-url, profile_url={profile_url[:80]}...")
    else:
        cmd_mode = ["--red-id", red_id]
        print(f"  [collect] mode=red-id, red_id={red_id}")

    r = subprocess.run([
        sys.executable, str(SKILL_DIR / "collect.py"),
        *cmd_mode,
        "--date", target_date,
        "--out", str(OUTPUT_ROOT),
    ], capture_output=True, text=True, encoding="utf-8", timeout=3600)

    if r.returncode != 0:
        raise RuntimeError(
            f"collect.py 退出码 {r.returncode}\n"
            f"stdout: {r.stdout[-800:]}\nstderr: {r.stderr[-500:]}"
        )

    json_files = list(OUTPUT_ROOT.rglob(f"*_{target_date}.json"))
    if not json_files:
        raise RuntimeError(f"collect.py 完成但找不到输出 JSON (匹配 *_{target_date}.json)")
    return json_files[-1]


def collect_account(account, target_date):
    """
    对单个账号执行采集+同步, 返回 stats dict 或 raise。

    流程:
    1. 如果 profile_url 含 xsec_token: 直接 --profile-url 模式采集
    2. 如果不含 token 或步骤 1 失败 (token 过期): 切到 --red-id 模式重试
    3. 重试成功后把新 URL 回写到飞书账号表
    4. 拿 collect.py 输出 JSON 调 sync_to_lark.py 同步飞书
    """
    profile_url = account["profile_url"]
    red_id = account["redId"]
    record_id = account.get("record_id")
    refreshed_url = None  # 刷新拿到的新 URL (如果有)
    used_fallback = False

    # 1. 先用 profile_url 试一次 (如果有的话)
    try:
        json_file = _run_collect(profile_url, red_id, target_date)
    except RuntimeError as e:
        err_msg = str(e)
        # 判断是不是 token 过期类错误
        is_token_error = any(kw.lower() in err_msg.lower() for kw in TOKEN_EXPIRED_KEYWORDS)

        if not account["needs_refresh"] and not is_token_error:
            # 已用 profile_url 但失败且不是 token 问题, 直接抛
            raise

        print(f"  [retry] profile-url 模式失败, 切换到 --red-id 自动刷新模式")
        print(f"  [retry] 原因: {err_msg[:200]}")
        used_fallback = True

        # 2. 切到 --red-id 模式重试
        json_file = _run_collect("", red_id, target_date)

    # 3. 如果用了 fallback 模式, 从输出 JSON 里提取最新的 profile_url 回写到飞书
    if used_fallback:
        try:
            with open(json_file, "rb") as f:
                collected = json.loads(f.read().decode("utf-8"))
            account_data = collected.get("account", {})
            refreshed_url = account_data.get("profile_url_with_token", "")
            if refreshed_url and record_id:
                print(f"  [writeback] 把新 URL 回写到飞书账号表 (record_id={record_id})")
                update_account_profile_url(record_id, refreshed_url)
        except Exception as e:
            print(f"  [writeback] 回写飞书失�� (不影响采集结果): {e}")

    # 4. 同步飞书
    with open(json_file, "rb") as f:
        collected = json.loads(f.read().decode("utf-8"))
    notes = collected.get("matched_notes", [])
    note_count = len(notes)
    comment_count = sum(n.get("comments", {}).get("count", 0) for n in notes)
    image_count = sum(len(n.get("image_files", [])) for n in notes)

    print(f"  [sync] python sync_to_lark.py --input {json_file.name}")
    r2 = subprocess.run([
        sys.executable, str(SKILL_DIR / "sync_to_lark.py"),
        "--input", str(json_file),
        "--data-root", str(OUTPUT_ROOT),
    ], capture_output=True, text=True, encoding="utf-8", timeout=1800, cwd=str(OUTPUT_ROOT))
    if r2.returncode != 0:
        raise RuntimeError(
            f"sync_to_lark.py 退出码 {r2.returncode}\n"
            f"stdout: {r2.stdout[-500:]}\nstderr: {r2.stderr[-500:]}"
        )

    return {
        "note_count": note_count,
        "comment_count": comment_count,
        "image_count": image_count,
        "has_published": note_count > 0,
        "used_fallback": used_fallback,
        "refreshed_url": refreshed_url,
    }


def main():
    print(f"=== xhs-collector v2.1 每日定时任务启动 ===")
    print(f"当前北京时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: 随机启动延迟
    jitter_sec = random.randint(STARTUP_JITTER_MIN * 60, STARTUP_JITTER_MAX * 60)
    print(f"\n[1/5] 随机等待 {jitter_sec} 秒 ({jitter_sec // 60} 分 {jitter_sec % 60} 秒)...")
    for remaining in range(jitter_sec, 0, -30):
        print(f"  剩余 {remaining} 秒", flush=True)
        time.sleep(min(30, remaining))

    # Step 2: 计算目标日期
    target_date = get_beijing_yesterday()
    print(f"\n[2/5] 目标日期 (北京时间昨天): {target_date}")

    # Step 3: 扫描账号表
    print(f"\n[3/5] 扫描飞书账号表...")
    accounts = list_accounts()
    refresh_count = sum(1 for a in accounts if a["needs_refresh"])
    print(f"  共 {len(accounts)} 个有效账号, 其中 {refresh_count} 个需要刷新 token")
    for a in accounts:
        flag = " [needs-refresh]" if a["needs_refresh"] else ""
        print(f"    - {a['redId']} ({a['nickname']}){flag}")
    if not accounts:
        print("  无账号可采集, 任务结束")
        return

    # Step 4: 日志表
    print(f"\n[4/5] 创建日志记录...")
    log_rid = create_log_record(target_date, len(accounts), jitter_sec)
    print(f"  log record_id: {log_rid}")

    # Step 5: 逐账号采集
    print(f"\n[5/5] 开始采集 (账号间隔 {ACCOUNT_GAP_MIN_MIN}-{ACCOUNT_GAP_MAX_MIN} 分钟)...")
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
            print(f"  账号间隔等待 {gap} 秒 ({gap // 60} 分)...")
            for remaining in range(gap, 0, -60):
                print(f"    剩余 {remaining} 秒", flush=True)
                time.sleep(min(60, remaining))

        try:
            stats = collect_account(acc, target_date)
            success_count += 1
            total_notes += stats["note_count"]
            total_comments += stats["comment_count"]
            total_images += stats["image_count"]
            extra = " (fallback 刷新 token 成功)" if stats["used_fallback"] else ""
            if not stats["has_published"]:
                print(f"  [ok] 该账号昨日无笔记发布, 静默跳过{extra}")
            else:
                print(f"  [ok] 采集 {stats['note_count']} 篇笔记 / {stats['comment_count']} 评论 / {stats['image_count']} 图片{extra}")
        except Exception as e:
            fail_count += 1
            err_msg = str(e)
            failed_accounts.append(f"{acc['redId']}:{acc['nickname']} - {err_msg[:200]}")
            error_details.append(f"=== {acc['redId']} ({acc['nickname']}) ===\n{err_msg}\n")
            print(f"  [FAIL] {err_msg[:400]}")
            print(f"\n  遇错即停策略: 终止后续账号采集")
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
        print(f"\n  任务失败, 请检查飞书日志表 record_id={log_rid}")
        print(f"飞书 URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")
        sys.exit(1)


if __name__ == "__main__":
    main()
