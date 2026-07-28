#!/usr/bin/env python
# -*- coding: utf-8 -*-
# xhs-collector 每日定时调度脚本 (v3 - 全 Edge 架构)
#
# 由 WorkBuddy automation 在指定时间触发执行。
# 流程:
#     1. 启动后随机等待 1~10 分钟 (模拟人类不规律作息)
#     2. 扫描飞书账号表, 拿到所有账号的 redId
#     3. 计算目标日期 (北京时间昨天)
#     4. 在飞书日志表里建一条 running 记录
#     5. 逐个账号调用 collect_v3.py (全 Edge 采集) + sync_to_lark.py
#     6. 账号之间随机 5~8 分钟间隔
#     7. 任何账号失败 → 立即停止, 更新日志表为 failed
#     8. 全部成功 → 更新日志表为 success
#
# v3 变化 (2026-07-28):
#   - 改用 collect_v3.py (全 Edge 单浏览器架构，弃用 MCP)
#   - 修复 3 个 bug:
#     #1 删除所有 --yes 参数 (record-batch-update 不支持)
#     #2 run_lark 检查返回值 ok 字段，失败时打印 warning
#     #3 采集前先检查登录态，未登录直接退出不请求
#   - 新增 ensure_edge 检查（确保 Edge CDP 9222 在运行）
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
# Node.js 包装脚本，解决 Windows 下 Python subprocess 传中文参数的 GBK 编码问题
# （P1-2: 跟 sync_to_lark.py 对齐，避免日志/错误信息含中文时崩溃）
NODE = r"C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2\node.exe"
LARK_RUN_JS = str(Path(__file__).parent / "lark_run.js")
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_ACCOUNT = "tbl9YZx9XsG1RoDN"
TBL_LOG = "tbltPTsDTPByT4oz"

SKILL_DIR = Path(r"C:\Users\Administrator\.workbuddy\skills\xhs-collector\scripts")
OUTPUT_ROOT = Path(r"C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09\xhs_scheduler_output")
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

PACING_API_SEC = 1.5                # 飞书 API 间隔
# P1-3: 启动抖动从 1-10 分钟扩大到 1-30 分钟（减小触发窗口可识别性）
STARTUP_JITTER_MIN = 1              # 启动随机等待最小分钟
STARTUP_JITTER_MAX = 30             # 启动随机等待最大分钟（原 10 → 30）
# P2: 账号间隔从 5-8 分钟拉大到 30-90 分钟，跨小时，降低批量采集特征
# （单账号场景下不触发，多账号扩盘时生效）
ACCOUNT_GAP_MIN_MIN = 30            # 账号间隔最小分钟
ACCOUNT_GAP_MAX_MIN = 90            # 账号间隔最大分钟
BEIJING_TZ = timezone(timedelta(hours=8))


def run_lark(args, timeout=30):
    """调用 lark-cli，返回解析后的 dict。检查 ok 字段（修复 bug #2）。
    P1-2: 改用 lark_run.js 包装，解决 Windows GBK 编码问题（中文日志/错误信息）。
    """
    # 把 --json 的值写入临时文件，用 @file 占位符传给 lark_run.js
    # lark_run.js 读文件内容替换占位符，通过 Node CreateProcessW 传给 lark-cli（避开 GBK）
    tmp_files = []
    final_args = []
    i = 0
    while i < len(args):
        if args[i] == "--json" and i + 1 < len(args):
            import uuid
            tmp = Path("_tmp_lark_json") / f"sch_{uuid.uuid4().hex[:8]}.json"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(args[i+1], encoding="utf-8")
            tmp_files.append(tmp)
            final_args.append("--json")
            final_args.append(f"@{tmp.absolute()}")
            i += 2
        else:
            final_args.append(args[i])
            i += 1

    has_json = any(str(a).startswith("@") for a in final_args)
    if has_json:
        cmd = [NODE, LARK_RUN_JS, str(tmp_files[0].absolute())] + final_args
    else:
        cmd = [LARK] + final_args

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out_bytes, err_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_bytes, err_bytes = proc.communicate()
        out = ""
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                out = out_bytes.decode(enc); break
            except UnicodeDecodeError: continue
        try:
            result = json.loads(out) if out.strip() else {"_stderr": err_bytes.decode("utf-8", errors="replace")}
        except json.JSONDecodeError:
            result = {"_raw": out[:500]}
        # 修复 bug #2：检查 ok 字段，失败时打印 warning（不再静默吞错）
        if isinstance(result, dict) and result.get("ok") is False:
            err = result.get("error", {})
            print(f"  [lark-warn] API 失败: {err.get('type', '?')} / {err.get('message', '?')[:150]}")
        return result
    except Exception as e:
        return {"_error": str(e)}
    finally:
        for tmp in tmp_files:
            try: tmp.unlink()
            except Exception: pass


def get_beijing_yesterday():
    now_bj = datetime.now(BEIJING_TZ)
    yesterday = now_bj - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def _extract_text(val):
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts = []
        for seg in val:
            if isinstance(seg, dict):
                if seg.get("type") == "url" and seg.get("link"):
                    return seg["link"]
                parts.append(seg.get("text", seg.get("name", "")))
            else:
                parts.append(str(seg))
        return "".join(parts)
    return str(val)


def check_edge_cdp():
    """检查 Edge CDP 9222 是否在运行。"""
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:9222/json/version", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return True, data.get("Browser", "?")
    except Exception:
        return False, None


def check_login_status():
    """采集前先检查 Edge 里的登录态（修复 bug #3 + P1-1）。
    三重检查：
      ① web_session cookie 存在且足够长
      ② 导航 explore 页无登录按钮、未被重定向到 /login
      ③ user/me API 返回 success + user_id + guest:false（关键，7-28 教训）
    """
    log("[precheck] 检查 Edge 登录态...")
    edge_ok, _ = check_edge_cdp()
    if not edge_ok:
        log("[precheck] ❌ Edge CDP 9222 未运行")
        return False

    # 用 Playwright 做完整三重检查（跟 collect_v3.check_login 对齐）
    check_code = '''
import asyncio, json
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0] if b.contexts else None
        if not ctx:
            print(json.dumps({"ok": False, "reason": "no context"}))
            return
        # ① cookie 检查
        cookies = await ctx.cookies()
        session = next((c for c in cookies if c["name"] == "web_session" and len(c["value"]) > 20), None)
        if not session:
            print(json.dumps({"ok": False, "reason": "no web_session (or too short)", "stage": "cookie"}))
            await b.close()
            return
        # 找到小红书页面
        page = None
        for pg in ctx.pages:
            if "xiaohongshu.com" in pg.url:
                page = pg
                break
        if not page:
            page = await ctx.new_page()
        # ② 页面特征检查
        await page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        await asyncio.sleep(3)
        login_btn = await page.query_selector('.login-btn, [class*="login-container"], .side-bar .login-btn')
        if login_btn:
            print(json.dumps({"ok": False, "reason": "login button visible", "stage": "page_feature"}))
            await b.close()
            return
        if "/login" in page.url:
            print(json.dumps({"ok": False, "reason": "redirected to login", "stage": "page_feature", "url": page.url}))
            await b.close()
            return
        # ③ user/me API 探测（关键：查 guest）
        captured = []
        def on_resp(resp):
            async def grab():
                try:
                    if "/api/sns/web/v2/user/me" in resp.url or "/api/sns/web/v1/user/me" in resp.url:
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct:
                            body = await resp.text()
                            captured.append(json.loads(body))
                except Exception:
                    pass
            return grab()
        ctx.on("response", lambda r: asyncio.create_task(on_resp(r)))
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(5)
        if not captured:
            print(json.dumps({"ok": False, "reason": "no user/me response", "stage": "api_probe"}))
            await b.close()
            return
        me = captured[0]
        me_data = me.get("data", {})
        me_guest = me_data.get("guest", False)
        me_success = me.get("success", False)
        me_uid = me_data.get("user_id", "")
        if not me_success or not me_uid:
            print(json.dumps({"ok": False, "reason": f"user/me invalid: success={me_success} uid={me_uid}", "stage": "api_probe"}))
            await b.close()
            return
        if me_guest:
            print(json.dumps({"ok": False, "reason": "guest:true - cookie exists but server treats as guest", "stage": "api_probe", "guest": True}))
            await b.close()
            return
        print(json.dumps({"ok": True, "nickname": me_data.get("nickname","?"), "session": session["value"][:16]}))
        await b.close()

asyncio.run(check())
'''
    try:
        r = subprocess.run(
            [sys.executable, "-c", check_code],
            capture_output=True, text=True, encoding="utf-8", timeout=45
        )
        out = r.stdout.strip()
        if out:
            data = json.loads(out)
            if data.get("ok"):
                log(f"[precheck] ✅ 登录态正常 ({data.get('nickname','?')}, session={data.get('session','?')}...)")
                return True
            else:
                stage = data.get("stage", "?")
                reason = data.get("reason", "?")
                log(f"[precheck] ❌ 未登录 [stage={stage}]: {reason}")
                if data.get("guest"):
                    log("[precheck]   ⚠️ cookie 存在但服务端判定为游客 — 需要重新扫码登录")
                return False
        else:
            log(f"[precheck] ❌ 检查脚本无输出 (stderr: {r.stderr[:200]})")
    except subprocess.TimeoutExpired:
        log("[precheck] ❌ 检查脚本超时")
    except Exception as e:
        log(f"[precheck] 检查异常: {e}")
    return False


def list_accounts():
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

        if not red_id:
            continue

        accounts.append({
            "redId": red_id,
            "nickname": nickname,
            "record_id": rid,
        })
    return accounts


def update_log_record(log_rid, **kwargs):
    """更新日志表记录（修复 bug #1：删除 --yes 参数）。"""
    if not log_rid:
        return
    fields = {k: v for k, v in kwargs.items() if v is not None}
    if not fields:
        return
    time.sleep(PACING_API_SEC)
    payload = {"update_records": {log_rid: fields}}
    # 修复 bug #1：没有 --yes 参数（record-batch-update 不支持）
    run_lark([
        "base", "+record-batch-update",
        "--base-token", BASE_TOKEN, "--table-id", TBL_LOG,
        "--json", json.dumps(payload, ensure_ascii=False),
        "--as", "user",
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


def _run_collect_v3(red_id, target_date):
    """调用 collect_v3.py（全 Edge 架构）。"""
    print(f"  [collect] mode=v3-edge, red_id={red_id}")
    r = subprocess.run([
        sys.executable, str(SKILL_DIR / "collect_v3.py"),
        "--red-id", red_id,
        "--date", target_date,
        "--out", str(OUTPUT_ROOT),
    ], capture_output=True, text=True, encoding="utf-8", timeout=3600)

    if r.returncode != 0:
        raise RuntimeError(
            f"collect_v3.py 退出码 {r.returncode}\n"
            f"stdout: {r.stdout[-1000:]}\nstderr: {r.stderr[-500:]}"
        )

    # 找输出 JSON（兼容旧版路径格式 redId_nickname_date.json）
    json_files = list(OUTPUT_ROOT.rglob(f"*_{target_date}.json"))
    if not json_files:
        raise RuntimeError(f"collect_v3.py 完成但找不到输出 JSON (匹配 *_{target_date}.json)")
    return json_files[-1]


def log(msg):
    ts = datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def collect_account(account, target_date):
    """对单个账号执行采集+同步，返回 stats dict 或 raise。"""
    red_id = account["redId"]

    # 1. 采集（collect_v3.py 用 --red-id，内部自动刷新 token）
    json_file = _run_collect_v3(red_id, target_date)

    # 2. 统计
    with open(json_file, "rb") as f:
        collected = json.loads(f.read().decode("utf-8"))
    notes = collected.get("matched_notes", [])
    note_count = len(notes)
    comment_count = sum(n.get("comments", {}).get("count", 0) for n in notes)
    image_count = sum(len(n.get("image_files", [])) for n in notes)

    # 3. 同步飞书
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
    }


def main():
    print(f"=== xhs-collector v3 每日定时任务启动 ===")
    print(f"当前北京时间: {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: 随机启动延迟
    jitter_sec = random.randint(STARTUP_JITTER_MIN * 60, STARTUP_JITTER_MAX * 60)
    print(f"\n[1/6] 随机等待 {jitter_sec} 秒 ({jitter_sec // 60} 分 {jitter_sec % 60} 秒)...")
    for remaining in range(jitter_sec, 0, -30):
        print(f"  剩余 {remaining} 秒", flush=True)
        time.sleep(min(30, remaining))

    # Step 2: 计算目标日期
    target_date = get_beijing_yesterday()
    print(f"\n[2/6] 目标日期 (北京时间昨天): {target_date}")

    # Step 3: 前置检查（修复 bug #3：采集前先检查登录态）
    print(f"\n[3/6] 前置检查...")
    edge_ok, browser_ver = check_edge_cdp()
    if not edge_ok:
        print(f"  ❌ Edge CDP 9222 未运行！请先运行 ensure_edge.ps1")
        sys.exit(1)
    print(f"  Edge CDP OK (browser={browser_ver})")

    if not check_login_status():
        print(f"  ❌ Edge 未登录小红书！请扫码登录小号")
        print(f"     启动 Edge: powershell -ExecutionPolicy Bypass -File \"{SKILL_DIR.parent / 'ensure_edge.ps1'}\"")
        sys.exit(1)

    # Step 4: 扫描账号表
    print(f"\n[4/6] 扫描飞书账号表...")
    accounts = list_accounts()
    print(f"  共 {len(accounts)} 个有效账号")
    for a in accounts:
        print(f"    - {a['redId']} ({a['nickname']})")
    if not accounts:
        print("  无账号可采集，任务结束")
        return

    # Step 5: 日志表
    print(f"\n[5/6] 创建日志记录...")
    log_rid = create_log_record(target_date, len(accounts), jitter_sec)
    print(f"  log record_id: {log_rid}")

    # Step 6: 逐账号采集
    print(f"\n[6/6] 开始采集 (账号间隔 {ACCOUNT_GAP_MIN_MIN}-{ACCOUNT_GAP_MAX_MIN} 分钟)...")
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
            if not stats["has_published"]:
                print(f"  [ok] 该账号昨日无笔记发布，静默跳过")
            else:
                print(f"  [ok] 采集 {stats['note_count']} 篇笔记 / {stats['comment_count']} 评论 / {stats['image_count']} 图片")
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
        print(f"\n  任务失败，请检查飞书日志表 record_id={log_rid}")
        print(f"飞书 URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")
        sys.exit(1)


if __name__ == "__main__":
    main()
