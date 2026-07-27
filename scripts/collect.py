#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs-collector · 主采集脚本 (v2 - 适配 xiaohongshu-mcp v2.0.0+)

用法:
    python collect.py --profile-url "https://www.xiaohongshu.com/user/profile/XXXX?xsec_token=YYYY" --date 2026-07-26 [--out ./out]

关键变化 (v1 → v2):
    - 输入参数: --red-id → --profile-url（完整主页URL，必须带 xsec_token）
    - 原因: xiaohongshu-mcp v2.0.0 的 user_profile 强制要求 user_id(24位hex) + xsec_token
            redId(8-11位数字) 既不是 user_id 也无法换取 xsec_token，因此废弃 redId 作为输入
    - MCP 调用必须带 Accept: application/json, text/event-stream 头
    - user_profile 返回结构: userBasicInfo / interactions / feeds (不再是 basic_info/notes)
    - get_feed_detail 参数: feed_id (不再是 note_id)
    - feeds 列表里没有时间字段，必须逐篇拉详情才能按天过滤

输出:
    <out>/<redId>_<nickname>/<redId>_<nickname>_<date>.json
    <out>/<redId>_<nickname>/images/<note_id>_<idx>.webp

风控规则:
    - 任意两次 MCP 调用之间 ≥1.5 秒
    - 命中笔记之间 ≥30 秒
    - 风控关键词命中立即停止: 风控/异常/blocked/forbidden/请稍后再试/verify/登录已过期
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MCP_URL = "http://localhost:18060/mcp"
PACING_API_SEC = 1.5        # 任意两次 MCP API 调用间隔
PACING_NOTE_SEC = 30.0      # 命中笔记之间间隔
PACING_BATCH_SEC = 15.0     # 每拉取 10 篇详情小休一次
RISK_KEYWORDS = ["风控", "异常", "blocked", "forbidden", "请稍后再试",
                 "verify", "登录已过期", "login", "访问被拒绝"]

BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


def log(msg):
    ts = datetime.datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============== MCP 调用 ==============

def mcp_post(payload, headers=None, timeout=180):
    """发送 POST，返回 (parsed_body, response_headers)。"""
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",  # v2.0.0 必须带，否则 400
    }
    if headers:
        h.update(headers)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")[:500]
        raise RuntimeError(f"HTTP {e.code}: {body}")


def mcp_init():
    """初始化 MCP 会话，返回 session_id。"""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xhs-collector", "version": "2.0.0"},
        },
    }
    body, headers = mcp_post(payload, timeout=15)
    sid = None
    for k, v in headers.items():
        if k.lower() == "mcp-session-id":
            sid = v
            break
    if not sid:
        raise RuntimeError(f"未拿到 Mcp-Session-Id, headers={headers}")
    # 发送 initialized 通知
    time.sleep(PACING_API_SEC)
    try:
        mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                 headers={"Mcp-Session-Id": sid}, timeout=10)
    except Exception:
        pass  # 通知允许失败
    return sid


def check_risk(text):
    """风控关键词检测，命中则抛 RuntimeError。"""
    lower = text.lower()
    for kw in RISK_KEYWORDS:
        if kw in lower or kw in text:
            raise RuntimeError(f"⚠️ 风控关键词命中: '{kw}' — 立即停止")


def mcp_call(sid, tool_name, arguments, timeout=180):
    """调用 MCP 工具，返回解析后的对象。"""
    payload = {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp, _ = mcp_post(payload, headers={"Mcp-Session-Id": sid}, timeout=timeout)
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    text = resp["result"]["content"][0]["text"]
    check_risk(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_text": text}


# ============== URL 解析 ==============

def parse_profile_url(url):
    """
    从主页 URL 提取 user_id 和 xsec_token。
    URL 格式: https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token=...&...
    user_id 是 24 位 hex。
    """
    parsed = urllib.parse.urlparse(url)
    # path: /user/profile/64632e46000000001002b7c5
    m = re.match(r"^/user/profile/([a-f0-9]{24})$", parsed.path)
    if not m:
        raise RuntimeError(f"URL 路径无法解析 user_id: {parsed.path}（需要 24 位 hex）")
    user_id = m.group(1)

    # query 里的 xsec_token（注意可能 URL 编码过）
    qs = urllib.parse.parse_qs(parsed.query)
    xsec_token = qs.get("xsec_token", [""])[0]
    if not xsec_token:
        raise RuntimeError(f"URL 里没有 xsec_token 参数: {url}")
    # URL 解码（%3D → =）
    xsec_token = urllib.parse.unquote(xsec_token)
    return user_id, xsec_token


# ============== 业务逻辑 ==============

def fetch_profile(sid, user_id, xsec_token):
    """调用 user_profile 拿账号信息和笔记列表 (v2.0.0 新 schema)。"""
    data = mcp_call(sid, "user_profile", {
        "user_id": user_id,
        "xsec_token": xsec_token,
    })

    info = data.get("userBasicInfo", {})
    interactions_list = data.get("interactions", [])
    feeds = data.get("feeds", [])

    # interactions 是数组: [{type:'follows',count:'3'}, ...]
    interactions = {}
    for it in interactions_list:
        t = it.get("type", "")
        c = it.get("count", "0")
        try:
            interactions[t] = int(c)
        except (ValueError, TypeError):
            interactions[t] = 0

    account = {
        "redId": str(info.get("redId", "")),
        "user_id": user_id,
        "nickname": info.get("nickname", ""),
        "desc": info.get("desc", "") or "",
        "ip_location": info.get("ipLocation", "") or "",
        "avatar": info.get("imageb") or info.get("images", "") or "",
        "interactions": {
            "follows": interactions.get("follows", 0),
            "fans": interactions.get("fans", 0),
            "interaction": interactions.get("interaction", 0),
        },
        # 保留原始 URL 供飞书表存储
        "profile_url_with_token": f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token={urllib.parse.quote(xsec_token, safe='')}",
    }
    return account, feeds


def fetch_detail(sid, feed_id, xsec_token):
    """调用 get_feed_detail 拿笔记详情 + 评论 (v2.0.0 新 schema, 参数名是 feed_id)。"""
    return mcp_call(sid, "get_feed_detail", {
        "feed_id": feed_id,
        "xsec_token": xsec_token,
        # 不主动加载全部评论，只取首页 10 条，避免触发风控
        "load_all_comments": False,
    })


def in_date_range(ts_ms, target_date_str):
    """毫秒时间戳(北京时间)是否落在 target_date_str (YYYY-MM-DD) 当天。"""
    if not ts_ms:
        return False, None
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, BEIJING_TZ)
    return dt.strftime("%Y-%m-%d") == target_date_str, dt


def download_image(url, save_path):
    """下载图片到本地。"""
    if not url or not url.startswith("http"):
        return False
    save_path.parent.mkdir(parents=True, exist_ok=True)
    if save_path.exists():
        return True
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.xiaohongshu.com/",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            save_path.write_bytes(resp.read())
        return True
    except Exception as e:
        log(f"    图片下载失败 {url[:60]}: {e}")
        return False


def safe_filename(name):
    """生成文件名安全的字符串。"""
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:50]


def main():
    ap = argparse.ArgumentParser(description="xhs-collector v2 - 采集指定账号指定日期的笔记")
    ap.add_argument("--profile-url", required=True,
                    help="小红书主页完整 URL（必须带 xsec_token 参数）")
    ap.add_argument("--date", required=True,
                    help="目标日期 YYYY-MM-DD（北京时间）")
    ap.add_argument("--out", default="./xhs_output",
                    help="输出根目录（默认 ./xhs_output）")
    args = ap.parse_args()

    # 1. 解析 URL
    try:
        user_id, xsec_token = parse_profile_url(args.profile_url)
    except RuntimeError as e:
        log(f"❌ URL 解析失败: {e}")
        log(f"   需要的格式: https://www.xiaohongshu.com/user/profile/<24位hex>?xsec_token=...")
        sys.exit(1)

    log("=" * 60)
    log(f"开始采集 user_id={user_id}, date={args.date}")
    log("=" * 60)

    # 2. MCP 初始化 + 登录检查
    log("[1/5] 初始化 MCP 会话...")
    sid = mcp_init()
    log(f"      session_id = {sid}")

    log("[2/5] 检查登录状态...")
    time.sleep(PACING_API_SEC)
    login = mcp_call(sid, "check_login_status", {}, timeout=180)
    login_text = json.dumps(login, ensure_ascii=False)
    if "已登录" not in login_text and "true" not in login_text.lower():
        log(f"❌ 未登录，请先用 xiaohongshu-login-windows-amd64.exe 扫码")
        sys.exit(1)
    log("      登录正常")

    # 3. 拉账号主页 + feeds 列表
    log(f"[3/5] 拉取账号主页...")
    time.sleep(PACING_API_SEC)
    try:
        account, feeds = fetch_profile(sid, user_id, xsec_token)
    except RuntimeError as e:
        err = str(e)
        log(f"❌ user_profile 失败: {err}")
        if "xsec_token" in err or "token" in err.lower():
            log(f"   提示: xsec_token 可能已失效，请到小红书网页端打开该账号主页，复制浏览器地址栏的完整 URL 重新提供")
        sys.exit(2)

    red_id = account["redId"]
    nickname = account["nickname"]
    log(f"      账号: {nickname} (redId={red_id}, 粉丝={account['interactions']['fans']})")
    log(f"      主页笔记列表数: {len(feeds)}")

    out_root = Path(args.out).absolute()
    safe_nick = safe_filename(nickname) or "unknown"
    out_dir = out_root / f"{red_id}_{safe_nick}"
    img_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 4. 逐篇拉详情，按日期过滤
    log(f"[4/5] 逐篇拉详情过滤日期 {args.date}（共 {len(feeds)} 篇）...")
    matched = []
    for i, f in enumerate(feeds):
        feed_id = f.get("id")
        feed_token = f.get("xsecToken")
        if not feed_id or not feed_token:
            log(f"  [{i}] 跳过: 缺 id/token")
            continue

        try:
            time.sleep(PACING_API_SEC)
            detail = fetch_detail(sid, feed_id, feed_token)
            note = detail.get("data", {}).get("note", {})
            ts_ms = note.get("time")
            if not ts_ms:
                log(f"  [{i}] {feed_id} 无 time 字段,跳过")
                continue

            hit, dt = in_date_range(ts_ms, args.date)
            title = (note.get("title") or "")[:30]
            if hit:
                log(f"  [{i}] ✅ {dt.strftime('%H:%M')} {feed_id} {title}")
                matched.append({"feed": f, "detail": detail, "dt": dt})
                # 命中笔记间隔 30s+
                pause = 30.0 + (i % 5) * 3  # 30~42s
                log(f"      命中,休眠 {pause:.0f}s")
                time.sleep(pause)
            else:
                log(f"  [{i}] ❌ {dt.strftime('%m-%d %H:%M')} {feed_id} {title}")
                # 每 10 篇小休
                if (i + 1) % 10 == 0:
                    time.sleep(PACING_BATCH_SEC)

        except RuntimeError as e:
            err = str(e)
            log(f"  [{i}] ❌ ERROR {feed_id}: {err}")
            if any(kw in err for kw in RISK_KEYWORDS):
                log("⚠️⚠️⚠️ 疑似风控,立即终止!")
                sys.exit(3)
            time.sleep(5)

    log(f"\n      命中 {len(matched)} 篇")

    if not matched:
        log("该日期无笔记,输出空结果")
        result = {
            "collected_at": datetime.datetime.now(BEIJING_TZ).isoformat(),
            "target_date": args.date,
            "account": account,
            "matched_notes": [],
        }
        out_file = out_dir / f"{red_id}_{safe_nick}_{args.date}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"✅ 输出: {out_file}")
        return

    # 5. 整理 + 下载图片
    log("[5/5] 整理结果 + 下载图片...")
    notes_out = []
    for m in matched:
        note = m["detail"].get("data", {}).get("note", {})
        comments = m["detail"].get("data", {}).get("comments", {})

        # 下载图片
        img_files = []
        for idx, img in enumerate(note.get("imageList", [])):
            url = img.get("urlDefault") or img.get("urlPre") or img.get("url")
            if url:
                fname = f"{m['feed']['id']}_{idx}.webp"
                p = download_image(url, img_dir / fname)
                if p:
                    img_files.append(fname)
                time.sleep(0.5)

        notes_out.append({
            "note_id": note.get("noteId"),
            "xsec_token": m["feed"].get("xsecToken"),
            "type": note.get("type"),
            "title": note.get("title", "") or "",
            "desc": note.get("desc", "") or "",
            "time_ms": note.get("time"),
            "time_str": m["dt"].strftime("%Y-%m-%d %H:%M:%S"),
            "ip_location": note.get("ipLocation", "") or "",
            "interactions": note.get("interactInfo", {}),
            "image_files": img_files,
            "comments": {
                "count": len(comments.get("list", [])),
                "has_more": comments.get("hasMore", False),
                "list": comments.get("list", []),
            },
        })

    result = {
        "collected_at": datetime.datetime.now(BEIJING_TZ).isoformat(),
        "target_date": args.date,
        "account": account,
        "matched_notes": notes_out,
    }
    out_file = out_dir / f"{red_id}_{safe_nick}_{args.date}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    log("=" * 60)
    log(f"✅ 采集完成")
    log(f"   账号: {nickname} (redId={red_id})")
    log(f"   日期: {args.date}")
    log(f"   笔记: {len(notes_out)} 篇")
    log(f"   图片: {sum(len(n['image_files']) for n in notes_out)} 张")
    log(f"   评论: {sum(n['comments']['count'] for n in notes_out)} 条")
    log(f"   输出: {out_file}")
    log("=" * 60)


if __name__ == "__main__":
    main()
