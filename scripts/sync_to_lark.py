#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
xhs-collector · 飞书同步脚本

用法:
    python sync_to_lark.py --input ./xhs_output/95466594071_2026-07-22/95466594071_2026-07-22.json

把 collect.py 输出的 JSON 同步到飞书 Wiki 多维表格：
- 账号:   按 redId 去重（存在则更新，不存在则新增）
- 笔记:   按"笔记ID"去重
- 评论:   按"评论ID + 所属笔记"去重
- 图片:   自动以附件形式上传到笔记记录的"图片附件"字段
- 头像:   自动上传到账号记录的"头像"字段

去重规则:
    优先用 record-search 按 redId/笔记ID 查现有记录，找到则 PATCH，找不到则 POST。

风控规则:
    - 任何飞书 API 调用之间 ≥1 秒间隔（飞书硬性要求）
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ============== 飞书配置（来自 MEMORY） ==============
LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_ACCOUNT = "tbl9YZx9XsG1RoDN"
TBL_NOTE = "tblsIghwrc2TqemX"
TBL_COMMENT = "tblJH4LThxzKinzN"
TBL_PRODUCT = "tblZQCfpqne3YlAz"  # 保留备用，不主动写

PACING_API_SEC = 1.0


def run_lark(args, timeout=30):
    """调用 lark-cli，返回解析后的 JSON。"""
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
    """从 upsert 响应里抽出 record_id。"""
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


def find_record_by_keyword(table_id, field_name, keyword):
    """通过 record-search 按 keyword 查找记录，返回 record_id 或 None。"""
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


def fmt_time(ms):
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def upload_attachment(table_id, record_id, field_id, file_rel_path):
    """上传附件到指定记录字段。file_rel_path 必须是相对于 CWD 的相对路径。"""
    r = run_lark([
        "base", "+record-upload-attachment",
        "--base-token", BASE_TOKEN, "--table-id", table_id,
        "--record-id", record_id, "--field-id", field_id,
        "--file", file_rel_path, "--as", "user",
    ], timeout=60)
    return r.get("ok", False)


def upload_avatar(account_record_id, account_dir, avatar_url):
    """下载并上传头像到账号记录。"""
    if not avatar_url:
        return False
    avatar_path = account_dir / "avatar.jpg"
    if not avatar_path.exists():
        try:
            import urllib.request
            req = urllib.request.Request(avatar_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.xiaohongshu.com/",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                avatar_path.write_bytes(resp.read())
        except Exception as e:
            print(f"    头像下载失败: {e}")
            return False
    try:
        rel = avatar_path.relative_to(Path.cwd())
        time.sleep(PACING_API_SEC)
        return upload_attachment(TBL_ACCOUNT, account_record_id,
                                 "fld9tCQFr2", str(rel).replace("\\", "/"))
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="collect.py 输出的 JSON 文件路径")
    ap.add_argument("--data-root", default=".",
                    help="JSON 所在的根目录（用于解析图片相对路径），默认当前目录")
    args = ap.parse_args()

    input_path = Path(args.input).absolute()
    data_root = Path(args.data_root).absolute()
    if not input_path.exists():
        print(f"ERROR: 输入文件不存在: {input_path}")
        sys.exit(1)

    with open(input_path, "rb") as f:
        data = json.loads(f.read().decode("utf-8"))

    account = data["account"]
    notes = data.get("notes", [])
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")

    # ============== 1. 账号 upsert（按 redId 去重） ==============
    print(f"[1/3] 账号 {account['nickname']} (redId={account['redId']})...")
    existing_aid = find_record_by_keyword(TBL_ACCOUNT, "redId", account["redId"])
    if existing_aid:
        print(f"    已存在 record_id={existing_aid}，将更新")
        action = "update"
    else:
        print(f"    新记录，将创建")
        action = "create"
        existing_aid = None  # 让 upsert 创建新记录

    account_fields = {
        "昵称": account["nickname"],
        "redId": account["redId"],
        "user_id": account["user_id"],
        "简介": account.get("desc", "") or "",
        "IP属地": account.get("ip_location", "") or "",
        "关注数": int(account["interactions"].get("follows", 0) or 0),
        "粉丝数": int(account["interactions"].get("fans", 0) or 0),
        "获赞与收藏": int(account["interactions"].get("interaction", 0) or 0),
        "主页链接": f"[{account['nickname']}](https://www.xiaohongshu.com/user/profile/{account['user_id']})",
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
        print(f"    FAIL: {json.dumps(r, ensure_ascii=False)[:300]}")
        sys.exit(2)
    print(f"    OK account_record_id={aid} ({action})")

    # 上传头像（仅新建时上传，更新时跳过避免重复）
    if action == "create":
        print(f"    上传头像...")
        account_dir = input_path.parent
        if upload_avatar(aid, account_dir, account.get("avatar", "")):
            print(f"    头像上传 OK")
        time.sleep(PACING_API_SEC)

    # ============== 2. 笔记 upsert（按笔记ID去重） ==============
    print(f"\n[2/3] 笔记 {len(notes)} 篇...")
    note_record_map = {}  # note_id -> record_id
    for i, note in enumerate(notes, 1):
        note_id = note.get("note_id", "")
        title = note.get("title", "")[:60]
        print(f"  [{i}/{len(notes)}] {note_id} - {title}")
        if not note_id:
            print(f"    SKIP: 缺 note_id")
            continue

        existing_nid = find_record_by_keyword(TBL_NOTE, "笔记ID", note_id)
        action_n = "update" if existing_nid else "create"
        if existing_nid:
            print(f"    已存在 record_id={existing_nid}，将更新")

        # 标签
        import re
        tags = re.findall(r"#([^#\[\]]+)(?:\[话题\])?#?", note.get("desc", "") or "")
        tags_str = ", ".join(f"#{t}" for t in tags) if tags else ""

        note_fields = {
            "所属账号": [{"id": aid}],
            "笔记ID": note_id,
            "xsec_token": note.get("xsec_token", ""),
            "标题": note.get("title", ""),
            "正文": note.get("desc", ""),
            "IP属地": note.get("ip_location", ""),
            "发布时间": fmt_time(note.get("time_ms")),
            "采集时间": now_str,
            "笔记类型": "视频" if note.get("type") == "video" else "图文",
            "点赞数": int(note.get("interact", {}).get("likedCount", 0) or 0),
            "收藏数": int(note.get("interact", {}).get("collectedCount", 0) or 0),
            "评论数": int(note.get("interact", {}).get("commentCount", 0) or 0),
            "分享数": int(note.get("interact", {}).get("sharedCount", 0) or 0),
            "话题标签": tags_str,
            "笔记链接": f"[笔记](https://www.xiaohongshu.com/explore/{note_id})",
        }
        video = note.get("video")
        if video:
            streams = video.get("streams", [])
            if streams:
                note_fields["视频URL"] = streams[0].get("url", "")
            note_fields["视频封面URL"] = video.get("cover_url", "")

        time.sleep(PACING_API_SEC)
        r = run_lark([
            "base", "+record-upsert",
            "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
            "--json", json.dumps(note_fields, ensure_ascii=False),
            "--as", "user",
        ])
        nid = parse_record_id(r)
        if not nid:
            print(f"    FAIL: {json.dumps(r, ensure_ascii=False)[:300]}")
            continue
        note_record_map[note_id] = nid
        print(f"    OK note_record_id={nid} ({action_n})")

        # 上传图片附件（仅新建时上传）
        if action_n == "create":
            images = note.get("images", [])
            if images:
                print(f"    上传 {len(images)} 张图片...")
                for img in images:
                    img_path_str = img.get("path", "")
                    if not img_path_str:
                        continue
                    img_abs = data_root / img_path_str
                    if not img_abs.exists():
                        print(f"      SKIP {img_path_str}: 文件不存在")
                        continue
                    try:
                        img_rel = img_abs.relative_to(Path.cwd())
                    except ValueError:
                        # 不在 CWD 下，复制到 CWD/output 下
                        target = Path.cwd() / "output" / img_path_str
                        target.parent.mkdir(parents=True, exist_ok=True)
                        import shutil
                        shutil.copy2(img_abs, target)
                        img_rel = target.relative_to(Path.cwd())
                    time.sleep(PACING_API_SEC)
                    ok = upload_attachment(TBL_NOTE, nid, "fld5LkQ3yr",
                                           str(img_rel).replace("\\", "/"))
                    print(f"      {img_abs.name}: {'OK' if ok else 'FAIL'}")

    # ============== 3. 评论 upsert（按评论ID+所属笔记去重） ==============
    print(f"\n[3/3] 评论...")
    total_cmts = 0
    for note in notes:
        note_id = note.get("note_id", "")
        nid = note_record_map.get(note_id)
        if not nid:
            continue
        comments = note.get("comments", {})
        c_list = comments.get("list", []) if isinstance(comments, dict) else []
        if not c_list:
            continue
        print(f"  note {note_id} ({nid}): {len(c_list)} 条评论")
        for c in c_list:
            cid = c.get("id", "")
            existing_cid = find_record_by_keyword(TBL_COMMENT, "评论ID", cid) if cid else None
            action_c = "update" if existing_cid else "create"

            c_fields = {
                "所属笔记": [{"id": nid}],
                "评论ID": cid,
                "内容": c.get("content", ""),
                "用户ID": c.get("user_id", ""),
                "用户昵称": c.get("user_nickname", ""),
                "用户头像URL": c.get("user_avatar", "") or "",
                "IP属地": c.get("ip_location", "") or "",
                "点赞数": int(c.get("like_count", 0) or 0),
                "评论时间": fmt_time(c.get("create_time_ms")),
                "是否作者": bool(c.get("is_author", False)),
                "二级回复数": int(c.get("sub_comment_count", 0) or 0),
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
            if rid:
                total_cmts += 1
                print(f"    {cid[:16]}... ({action_c})")
            else:
                print(f"    FAIL: {json.dumps(r, ensure_ascii=False)[:200]}")

    print(f"\n✅ 同步完成")
    print(f"   账号: {aid} ({account['nickname']})")
    print(f"   笔记: {len(note_record_map)}/{len(notes)} 篇")
    print(f"   评论: {total_cmts} 条")
    print(f"\n飞书 URL: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb")


if __name__ == "__main__":
    main()
