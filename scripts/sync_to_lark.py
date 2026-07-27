#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs-collector · 飞书同步脚本 (v2)

用法:
    python sync_to_lark.py --input ./xhs_output/<redId>_<nick>/<redId>_<nick>_<date>.json

把 collect.py v2 输出的 JSON 同步到飞书 Wiki 多维表格：
- 账号:   按 redId 去重（存在则更新，不存在则新增）
- 笔记:   按"笔记ID"去重
- 评论:   按"评论ID + 所属笔记"去重
- 图片:   新建笔记时上传附件；更新时跳过（已在飞书里）
- 头像:   新建账号时上传附件

v2 关键变化:
- 输入 JSON 的 account 字段新增 profile_url_with_token（完整主页 URL，含 xsec_token）
- 飞书账号表"主页链接"字段改为存这个完整 URL（用于后续定时任务取回 token）
- 注意: token 有时效性,失效后用户需要重新打开网页端复制新 URL 更新飞书表

风控规则:
- ��何飞书 API 调用之间 ≥1.5 秒间隔
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ============== 飞书配置 ==============
LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_ACCOUNT = "tbl9YZx9XsG1RoDN"
TBL_NOTE = "tblsIghwrc2TqemX"
TBL_COMMENT = "tblJH4LThxzKinzN"
TBL_PRODUCT = "tblZQCfpqne3YlAz"  # 保留备用，不主动写

PACING_API_SEC = 1.5
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))


def log(msg):
    ts = datetime.datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def run_lark(args, timeout=60):
    cmd = [LARK] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        try:
            return json.loads(r.stdout) if r.stdout else {"_raw": "", "_stderr": r.stderr}
        except json.JSONDecodeError:
            return {"_raw": r.stdout[:500], "_stderr": r.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"_error": "timeout", "_args": args}


def parse_record_id(resp):
    if not resp.get("ok"):
        return None
    rec = resp.get("data", {}).get("record", {})
    if "record_id" in rec:
        return rec["record_id"]
    if "record_id_list" in rec and rec["record_id_list"]:
        return rec["record_id_list"][0]
    if "_record_id" in rec:
        return rec["_record_id"]
    return None


def find_record(table_id, field_name, keyword):
    r = run_lark([
        "base", "+record-search",
        "--base-token", BASE_TOKEN, "--table-id", table_id,
        "--keyword", keyword, "--search-field", field_name,
        "--limit", "5", "--as", "user", "--format", "json",
    ])
    if not r.get("ok"):
        return None
    ids = r.get("data", {}).get("record_id_list", [])
    return ids[0] if ids else None


def upload_attachment(table_id, record_id, field_id, file_abs_path):
    p = Path(file_abs_path)
    cwd = Path.cwd()
    try:
        rel = p.relative_to(cwd)
    except ValueError:
        # 复制到 CWD 下
        target = cwd / "_tmp_upload" / p.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        rel = target.relative_to(cwd)
    r = run_lark([
        "base", "+record-upload-attachment",
        "--base-token", BASE_TOKEN, "--table-id", table_id,
        "--record-id", record_id, "--field-id", field_id,
        "--file", str(rel).replace("\\", "/"), "--as", "user",
    ], timeout=90)
    return r.get("ok", False)


def fmt_time(ms):
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def safe_int(v):
    try:
        return int(v)
    except (ValueError, TypeError):
        return 0


def main():
    ap = argparse.ArgumentParser(description="xhs-collector v2 飞书同步")
    ap.add_argument("--input", required=True, help="collect.py v2 输出的 JSON 路径")
    ap.add_argument("--data-root", default=".",
                    help="JSON 所在根目录（用于解析图片相对路径）")
    args = ap.parse_args()

    input_path = Path(args.input).absolute()
    if not input_path.exists():
        print(f"ERROR: 输入文件不存在: {input_path}")
        sys.exit(1)

    with open(input_path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))

    account = data["account"]
    notes = data.get("matched_notes", [])
    img_dir = input_path.parent / "images"
    now_str = datetime.datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    # ============ 1. 账号 upsert（按 redId 去重）============
    log(f"=== 账号 upsert: {account['nickname']} (redId={account['redId']}) ===")
    time.sleep(PACING_API_SEC)
    existing_aid = find_record(TBL_ACCOUNT, "redId", account["redId"])
    action = "update" if existing_aid else "create"

    # 主页链接：存完整 URL（含 xsec_token），不要 markdown 格式
    # 这样定时任务能直接从飞书表里取回 token 用
    profile_url = account.get("profile_url_with_token") or \
                  f"https://www.xiaohongshu.com/user/profile/{account['user_id']}"

    account_fields = {
        "昵称": account["nickname"],
        "redId": account["redId"],
        "user_id": account["user_id"],
        "简介": account.get("desc", "") or "",
        "IP属地": account.get("ip_location", "") or "",
        "关注数": account["interactions"].get("follows", 0),
        "粉丝数": account["interactions"].get("fans", 0),
        "获赞与收藏": account["interactions"].get("interaction", 0),
        # 存纯 URL（非 markdown），方便程序读取
        "主页链接": profile_url,
        "采集来源": "xiaohongshu-mcp",
        "采集时间": now_str,
    }
    time.sleep(PACING_API_SEC)
    r = run_lark([
        "base", "+record-upsert",
        "--base-token", BASE_TOKEN, "--table-id", TBL_ACCOUNT,
        "--json", json.dumps(account_fields, ensure_ascii=False),
        "--as", "user",
    ])
    aid = parse_record_id(r)
    if not aid:
        log(f"❌ 账号 upsert 失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        sys.exit(2)
    log(f"✅ 账号 record_id={aid} ({action})")

    # 头像（仅新建时）
    if action == "create":
        avatar_url = account.get("avatar", "")
        if avatar_url:
            log("上传头像...")
            avatar_path = input_path.parent / "avatar.jpg"
            if not avatar_path.exists():
                try:
                    req = urllib.request.Request(avatar_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        avatar_path.write_bytes(resp.read())
                except Exception as e:
                    log(f"  头像下载失败: {e}")
            if avatar_path.exists():
                time.sleep(PACING_API_SEC)
                ok = upload_attachment(TBL_ACCOUNT, aid, "fld9tCQFr2", avatar_path)
                log(f"  头像: {'OK' if ok else 'FAIL'}")

    # ============ 2. 笔记 upsert ============
    log(f"\n=== 笔记 {len(notes)} 篇 ===")
    note_record_map = {}
    for i, note in enumerate(notes, 1):
        note_id = note.get("note_id", "")
        title = (note.get("title") or "")[:40]
        log(f"  [{i}/{len(notes)}] {note_id} - {title}")
        if not note_id:
            continue

        time.sleep(PACING_API_SEC)
        existing_nid = find_record(TBL_NOTE, "笔记ID", note_id)
        action_n = "update" if existing_nid else "create"

        inter = note.get("interactions", {})
        # 提取话题标签
        tags = re.findall(r"#([^#\[\]]+)(?:\[话题\])?#?", note.get("desc", "") or "")
        tags_str = ", ".join(f"#{t}" for t in tags) if tags else ""

        note_fields = {
            "所属账号": [{"id": aid}],
            "笔记ID": note_id,
            "xsec_token": note.get("xsec_token", ""),
            "标题": note.get("title", "") or "",
            "正文": note.get("desc", "") or "",
            "IP属地": note.get("ip_location", "") or "",
            "发布时间": fmt_time(note.get("time_ms")),
            "采集时间": now_str,
            "笔记类型": "视频" if note.get("type") == "video" else "图文",
            "点赞数": safe_int(inter.get("likedCount", 0)),
            "收藏数": safe_int(inter.get("collectedCount", 0)),
            "评论数": safe_int(inter.get("commentCount", 0)),
            "分享数": safe_int(inter.get("sharedCount", 0)),
            "话题标签": tags_str,
            "笔记链接": f"[笔记](https://www.xiaohongshu.com/explore/{note_id})",
        }

        time.sleep(PACING_API_SEC)
        r = run_lark([
            "base", "+record-upsert",
            "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
            "--json", json.dumps(note_fields, ensure_ascii=False),
            "--as", "user",
        ])
        nid = parse_record_id(r)
        if not nid:
            log(f"    ❌ FAIL: {json.dumps(r, ensure_ascii=False)[:200]}")
            continue
        note_record_map[note_id] = nid
        log(f"    ✅ note_record_id={nid} ({action_n})")

        # 图片（仅新建时）
        if action_n == "create":
            img_files = note.get("image_files", [])
            if img_files:
                log(f"    上传 {len(img_files)} 张图片...")
                for img_name in img_files:
                    img_path = img_dir / img_name
                    if not img_path.exists():
                        log(f"      SKIP {img_name}: 不存在")
                        continue
                    time.sleep(PACING_API_SEC)
                    ok = upload_attachment(TBL_NOTE, nid, "fld5LkQ3yr", img_path)
                    log(f"      {img_name}: {'OK' if ok else 'FAIL'}")

        # 评论
        comments = note.get("comments", {})
        c_list = comments.get("list", []) if isinstance(comments, dict) else []
        if c_list:
            log(f"    {len(c_list)} 条评论...")
            for c in c_list:
                cid = c.get("id", "")
                time.sleep(PACING_API_SEC)
                existing_cid = find_record(TBL_COMMENT, "评论ID", cid) if cid else None
                action_c = "update" if existing_cid else "create"

                user_info = c.get("userInfo", {})
                show_tags = c.get("showTags", [])
                is_author = "is_author" in show_tags

                c_fields = {
                    "所属笔记": [{"id": nid}],
                    "评论ID": cid,
                    "内容": c.get("content", "") or "",
                    "用户ID": user_info.get("userId", ""),
                    "用户昵称": user_info.get("nickname", ""),
                    "用户头像URL": user_info.get("avatar", "") or "",
                    "IP属地": c.get("ipLocation", "") or "",
                    "点赞数": safe_int(c.get("likeCount", 0)),
                    "评论时间": fmt_time(c.get("createTime")),
                    "是否作者": is_author,
                    "二级回复数": safe_int(c.get("subCommentCount", 0)),
                    "采集时间": now_str,
                }
                time.sleep(PACING_API_SEC)
                r = run_lark([
                    "base", "+record-upsert",
                    "--base-token", BASE_TOKEN, "--table-id", TBL_COMMENT,
                    "--json", json.dumps(c_fields, ensure_ascii=False),
                    "--as", "user",
                ])
                rid = parse_record_id(r)
                status = "✅" if rid else "❌"
                log(f"      {status} {cid[:16]}... ({action_c})")

    log(f"\n{'='*60}")
    log(f"✅ 同步完成")
    log(f"   账号: {account['nickname']} ({aid})")
    log(f"   笔记: {len(note_record_map)}/{len(notes)} 篇")
    log(f"   主页链接(含token): {profile_url[:80]}...")
    log(f"{'='*60}")
    log(f"飞书: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")


if __name__ == "__main__":
    main()
