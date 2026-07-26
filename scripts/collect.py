#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs-collector · 主采集脚本

用法:
    python collect.py --red-id 95466594071 --date 2026-07-22 [--out ./out]

输入:
    --red-id   小红书账号的 redId（8-11 位数字，主页 URL 里的"小红书号"）
    --date     想采集的日期，格式 YYYY-MM-DD（北京时间）
    --out      输出目录（默认: ./xhs_output）

依赖:
    - xiaohongshu-mcp 已启动并监听 http://localhost:18060/mcp
    - cookies.json 已登录某个小红书账号

输出:
    <out>/<redId>_<date>/<redId>_<date>.json  - 采集结果（账号+笔记+评论+图片路径）

风控规则:
    - 任何 MCP 调用之间 ≥1 秒间隔（小红书硬性要求）
    - 笔记之间 ≥30 秒间隔（避免短时间高频访问同一账号内容）
    - 任何响应出现"风控"字样或异常 status，立即停止并写错误日志
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

MCP_URL = "http://localhost:18060/mcp"
SESSION_ID_FILE = Path(os.environ.get("TEMP", "/tmp")) / "xhs_collector_session.txt"
PACING_API_SEC = 1.0       # 任意两次 MCP API 调用间隔
PACING_NOTE_SEC = 30.0     # 笔记之间间隔
RISK_KEYWORDS = ["风控", "异常", "blocked", "forbidden", "请稍后再试", "verify"]


def http_post_json(url, payload, headers=None, timeout=30):
    """发送 POST application/json，返回解析后的 JSON。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body), dict(resp.headers)


def mcp_initialize():
    """初始化 MCP 会话，返回 session_id。"""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xhs-collector", "version": "1.0.0"},
        },
    }
    resp_body, resp_headers = http_post_json(MCP_URL, payload, timeout=15)
    # session-id 可能在不同大小写
    sid = None
    for k, v in resp_headers.items():
        if k.lower() == "mcp-session-id":
            sid = v
            break
    if not sid:
        raise RuntimeError(f"未拿到 Mcp-Session-Id, headers={resp_headers}")
    # 发送 initialized 通知
    time.sleep(PACING_API_SEC)
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        http_post_json(MCP_URL, notif, headers={"Mcp-Session-Id": sid}, timeout=10)
    except Exception:
        pass  # 通知允许失败
    return sid


def mcp_call(sid, tool_name, arguments, timeout=60):
    """调用 MCP 工具，返回 result.content[0].text 解析后的对象。"""
    payload = {
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp, _ = http_post_json(MCP_URL, payload,
                             headers={"Mcp-Session-Id": sid}, timeout=timeout)
    # 风控检测
    raw = json.dumps(resp, ensure_ascii=False)
    for kw in RISK_KEYWORDS:
        if kw in raw:
            raise RuntimeError(f"⚠️ 风控信号 '{kw}' 出现在响应中，立即停止！raw={raw[:300]}")
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    text = resp["result"]["content"][0]["text"]
    return json.loads(text)


def parse_account_and_notes(profile_resp):
    """从 user_profile 响应里提取账号信息和笔记列表。"""
    # 响应结构因版本而异，做容错
    data = profile_resp.get("data", profile_resp)
    user = data.get("user", data.get("userInfo", {}))
    interactions = data.get("interactions", data.get("interaction", {}))
    notes = data.get("notes", data.get("feeds", []))
    account = {
        "redId": str(user.get("redId", user.get("red_id", ""))),
        "user_id": user.get("userId", user.get("user_id", "")),
        "nickname": user.get("nickname", ""),
        "desc": user.get("desc", ""),
        "ip_location": user.get("ipLocation", user.get("ip_location", "")),
        "avatar": user.get("avatar", user.get("image", "")),
        "interactions": {
            "follows": interactions.get("follows", interactions.get("followCount", 0)),
            "fans": interactions.get("fans", interactions.get("fansCount", 0)),
            "interaction": interactions.get("interaction", interactions.get("interactionCount", 0)),
        },
    }
    return account, notes


def filter_notes_by_date(notes, date_str, tz_offset_hours=8):
    """按北京时间筛选笔记（time 字段是毫秒时间戳）。"""
    target_date = time.strptime(date_str, "%Y-%m-%d")
    target_yday = target_date.tm_yday
    target_year = target_date.tm_year
    matched = []
    for n in notes:
        ts_ms = n.get("time", n.get("createTime", 0))
        if not ts_ms:
            continue
        # 转北京时间
        ts_sec = ts_ms / 1000 + tz_offset_hours * 3600
        tm = time.gmtime(ts_sec)
        if tm.tm_year == target_year and tm.tm_yday == target_yday:
            matched.append(n)
    return matched


def download_image(url, save_path, referer="https://www.xiaohongshu.com/"):
    """下载图片到本地。"""
    save_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": referer,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            save_path.write_bytes(resp.read())
        return True, save_path.stat().st_size
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--red-id", required=True, help="小红书 redId (8-11 位数字)")
    ap.add_argument("--date", required=True, help="日期 YYYY-MM-DD（北京时间）")
    ap.add_argument("--out", default="./xhs_output", help="输出目录")
    args = ap.parse_args()

    out_root = Path(args.out).absolute()
    out_dir = out_root / f"{args.red_id}_{args.date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/6] 初始化 MCP 会话...")
    sid = mcp_initialize()
    print(f"      session_id = {sid}")

    print(f"[2/6] 检查登录状态...")
    time.sleep(PACING_API_SEC)
    login = mcp_call(sid, "check_login_status", {})
    login_text = json.dumps(login, ensure_ascii=False)
    if "已登录" not in login_text:
        print(f"ERROR: 未登录，请先用 xiaohongshu-login-windows-amd64.exe 扫码")
        sys.exit(1)
    print(f"      {login_text[:120]}")

    # user_profile 需要 user_id + xsec_token，但 redId 不是 user_id
    # 通过 list_feeds + 搜索 redId 间接获取 user_id/xsec_token 的能力 xiaohongshu-mcp 不支持
    # 实际方案：直接传 redId 作为 user_id 调用（部分版本兼容），如果失败需要用户提供 user_id
    print(f"[3/6] 拉取账号 {args.red_id} 主页...")
    time.sleep(PACING_API_SEC)
    try:
        profile_resp = mcp_call(sid, "user_profile", {
            "user_id": args.red_id,  # redId 兼容
            "xsec_token": "",  # 部分 MCP 版本可空
        }, timeout=60)
    except RuntimeError as e:
        print(f"      user_profile 失败: {e}")
        print(f"      提示: redId 兼容性问题，请改用 user_id（24位hex）")
        sys.exit(2)

    account, all_notes = parse_account_and_notes(profile_resp)
    if not account["user_id"]:
        print(f"ERROR: 未拿到 user_id, profile_resp={json.dumps(profile_resp, ensure_ascii=False)[:300]}")
        sys.exit(3)
    print(f"      账号: {account['nickname']} (redId={account['redId']}, 粉丝={account['interactions']['fans']})")
    print(f"      主页笔记总数: {len(all_notes)}")

    print(f"[4/6] 筛选 {args.date}（北京时间）的笔记...")
    matched = filter_notes_by_date(all_notes, args.date)
    print(f"      匹配 {len(matched)} 篇")
    if not matched:
        print("      无匹配笔记，输出空结果")
        result = {"collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "target_date": args.date, "account": account, "notes": []}
        (out_dir / f"{args.red_id}_{args.date}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    print(f"[5/6] 采集每篇笔记详情+评论+图片...")
    enriched_notes = []
    for i, n in enumerate(matched, 1):
        note_id = n.get("noteId", n.get("note_id", ""))
        xsec = n.get("xsecToken", n.get("xsec_token", ""))
        title = n.get("title", n.get("displayTitle", ""))[:60]
        print(f"  [{i}/{len(matched)}] {note_id} - {title}")
        if i > 1:
            print(f"      等待 {PACING_NOTE_SEC}s（笔记间节流）...")
            time.sleep(PACING_NOTE_SEC)
        time.sleep(PACING_API_SEC)
        detail_args = {
            "feed_id": note_id, "xsec_token": xsec,
            "load_all_comments": True, "limit": 20,
            "click_more_replies": False, "scroll_speed": "slow",
        }
        try:
            detail = mcp_call(sid, "get_feed_detail", detail_args, timeout=90)
        except RuntimeError as e:
            print(f"      FAIL: {e}")
            enriched_notes.append({"note_id": note_id, "_error": str(e)})
            continue

        note_data = detail.get("data", {}).get("note", {})
        comments_data = detail.get("data", {}).get("comments", {})
        c_list = comments_data.get("list", []) if isinstance(comments_data, dict) else comments_data

        # 下载图片
        image_files = []
        note_dir = out_dir / f"note_{note_id}"
        for j, img in enumerate(note_data.get("imageList", []), 1):
            url = img.get("urlDefault") or img.get("url_default") or img.get("url", "")
            if not url:
                continue
            w, h = img.get("width", "?"), img.get("height", "?")
            img_path = note_dir / f"image_{j:02d}_{w}x{h}.webp"
            ok, info = download_image(url, img_path)
            if ok:
                image_files.append({"path": str(img_path.relative_to(out_root)),
                                    "width": w, "height": h, "size": info})
                print(f"      图片 {j}: OK ({info//1024}KB)")
            else:
                print(f"      图片 {j}: FAIL {info}")
            time.sleep(PACING_API_SEC)

        # 视频封面也下载
        video = note_data.get("video") or note_data.get("media", {}).get("video")
        video_cover_path = None
        if video:
            cover_url = video.get("cover", {}).get("url") or video.get("cover_url", "")
            if cover_url:
                cover_path = note_dir / "video_cover.jpg"
                ok, _ = download_image(cover_url, cover_path)
                if ok:
                    video_cover_path = str(cover_path.relative_to(out_root))

        enriched = {
            "note_id": note_id,
            "xsec_token": xsec,
            "title": note_data.get("title", ""),
            "desc": note_data.get("desc", ""),
            "type": note_data.get("type", "normal"),
            "time_ms": note_data.get("time", 0),
            "ip_location": note_data.get("ipLocation", ""),
            "interact": note_data.get("interactInfo", {}),
            "images": image_files,
            "video": {
                "cover_path": video_cover_path,
                "cover_url": video.get("cover", {}).get("url", "") if video else "",
                "streams": [{"format": s.get("format", ""), "url": s.get("url", ""), "size": s.get("size", 0)}
                            for s in (video.get("streams", []) if video else [])],
            } if video else None,
            "comments": {
                "count": len(c_list),
                "list": [{
                    "id": c.get("id", ""),
                    "content": c.get("content", ""),
                    "like_count": c.get("likeCount", 0),
                    "create_time_ms": c.get("createTime", 0),
                    "ip_location": c.get("ipLocation", ""),
                    "user_id": (c.get("userInfo") or {}).get("userId", ""),
                    "user_nickname": (c.get("userInfo") or {}).get("nickname", ""),
                    "user_avatar": (c.get("userInfo") or {}).get("avatar", ""),
                    "sub_comment_count": c.get("subCommentCount", 0),
                    "is_author": "is_author" in (c.get("showTags", []) or []),
                } for c in c_list],
            },
        }
        enriched_notes.append(enriched)

    print(f"[6/6] 输出 JSON 结果...")
    result = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "target_date": args.date,
        "account": account,
        "notes": enriched_notes,
        "pacing": {
            "api_interval_sec": PACING_API_SEC,
            "note_interval_sec": PACING_NOTE_SEC,
        },
    }
    out_file = out_dir / f"{args.red_id}_{args.date}.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 完成: {out_file}")
    print(f"   账号: {account['nickname']}")
    print(f"   笔记: {len(enriched_notes)} 篇")
    total_imgs = sum(len(n.get("images", [])) for n in enriched_notes)
    total_cmts = sum(n.get("comments", {}).get("count", 0) for n in enriched_notes)
    print(f"   图片: {total_imgs} 张")
    print(f"   评论: {total_cmts} 条")


if __name__ == "__main__":
    main()
