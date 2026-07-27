#!/usr/bin/env python
# -*- coding: utf-8 -*-
# llm_infer_product.py
#
# 用视觉 LLM 推测每条笔记具体在卖什么 (不是宽泛品类, 而是具体商品/服务名)。
# 流程:
#   1. 拉飞书笔记表全部记录
#   2. 每条下载��一张图片附件 → 转 PNG
#   3. 调 mcp__zai-vision-mcp__analyze_image (视觉 LLM) 推测
#   4. 解析 LLM 输出, 提取「具体商品」一句话
#   5. 批量回填到飞书笔记表「推测带货品类」字段 (覆盖之前的关键词匹配结果)
#
# 用法: python llm_infer_product.py [--max N] [--dry-run]
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

LARK = r"C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"
BASE_TOKEN = "KbmpbuXoiatEOcsnR5FcYtkJncL"
TBL_NOTE = "tblsIghwrc2TqemX"

PACING_API_SEC = 1.5
WORK_DIR = Path(r"C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09\xhs_llm_infer")
WORK_DIR.mkdir(parents=True, exist_ok=True)

# zai-vision-mcp 的工具调用是通过 MCP 协议, 我们用 curl 直调本地 mcp server
# 但更简单的方式是用 WorkBuddy 的 DeferExecuteTool, 这里我们是脚本环境, 改用
# 直接 HTTP 调用智谱的 GLM-4V API (需要 API key), 或者通过文件 I/O 跟主程序协作。
#
# 但这里有个简化方案: 因为 analyze_image 也支持远程 URL,
# 而飞书附件下载后是��地文件, 我们直接通过命令行触发 WorkBuddy agent 调用即可。
#
# 实际上, 在脚本里最干净的做法是: 用 ImageGen 工具 deferred 调用, 但这是图像生成。
# 分析图像需要 zai-vision-mcp__analyze_image, 这个只能在 WorkBuddy 主对话里调。
#
# 所以本脚本的作用是:
#   - 准备好每条笔记的 (record_id, image_path, title, desc, tags)
#   - 输出一个 JSON 任务清单
#   - 真正的 LLM 调用由上层 (主对话 / Agent) 按清单逐条执行
#
# 这样设计是因为 MCP 工具调用必须在 agent 上下文里, 不能在独立 Python 进程里直接调。

def extract_text(val):
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return "".join(s.get("text", "") if isinstance(s, dict) else str(s) for s in val)
    return str(val)


def list_notes():
    """返回 [{record_id, title, desc, tags, image_file_token, image_name}, ...]"""
    r = subprocess.run([
        LARK, "base", "+record-list",
        "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
        "--limit", "200", "--as", "user", "--format", "json",
    ], capture_output=True, text=True, encoding="utf-8", timeout=60)
    data = json.loads(r.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"record-list failed: {json.dumps(data, ensure_ascii=False)[:300]}")

    d = data["data"]
    rids = d.get("record_id_list", [])
    rows = d.get("data", [])
    fields = d.get("fields", [])
    idx = {f: i for i, f in enumerate(fields)}

    notes = []
    for rid, row in zip(rids, rows):
        title = extract_text(row[idx["标题"]] if "标题" in idx and idx["标题"] < len(row) else "")
        desc = extract_text(row[idx["正文"]] if "正文" in idx and idx["正文"] < len(row) else "")
        tags = extract_text(row[idx["话题标签"]] if "话题标签" in idx and idx["话题标签"] < len(row) else "")
        img_cell = row[idx["图片附件"]] if "图片附件" in idx and idx["图片附件"] < len(row) else None

        image_file_token = ""
        image_name = ""
        if isinstance(img_cell, list) and img_cell:
            first = img_cell[0]
            if isinstance(first, dict):
                image_file_token = first.get("file_token", "")
                image_name = first.get("name", "")

        notes.append({
            "record_id": rid,
            "title": title,
            "desc": desc,
            "tags": tags,
            "image_file_token": image_file_token,
            "image_name": image_name,
        })
    return notes


def download_and_convert(record_id, file_token, out_png_path):
    """下载飞书附件并转成 PNG。返回 True/False。"""
    webp_path = out_png_path.with_suffix(".webp")
    # 1. 下载
    r = subprocess.run([
        LARK, "base", "+record-download-attachment",
        "--base-token", BASE_TOKEN, "--table-id", TBL_NOTE,
        "--record-id", record_id, "--file-token", file_token,
        "--output", str(webp_path.relative_to(WORK_DIR)),
        "--overwrite", "--as", "user",
    ], capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=str(WORK_DIR))
    if not webp_path.exists():
        return False, f"download failed: {r.stdout[-200:]}"
    # 2. 转 PNG (PIL)
    try:
        from PIL import Image
        img = Image.open(webp_path)
        img.save(out_png_path)
        return True, ""
    except Exception as e:
        return False, f"convert failed: {e}"


def make_llm_prompt(title, desc, tags):
    return f"""这是一条小红书笔记的封面图。请结合图片和以下文字信息，推断这条笔记具体在卖什么商品或服务。

【笔记信息】
标题：{title}
正文：{desc[:500]}
话题标签：{tags[:200]}

请按以下格式严格输出（不要加其他内容）：

图片内容：[简短描述图片里看到了什么，30字以内]

具体商品：[一句话，越具体越好。例如"2026年执业药师考试全套备考资料包（网课+题库+教材）"，而不是宽泛的"知识付费"。如果没有明显在卖东西，写"无明显带货"]

把握程度：[高/中/低]

依据：[为什么这么判断，30字以内]"""


def parse_llm_output(output):
    """从 LLM 输出里提取「具体商品」那一行。"""
    if not output:
        return "无明显带货"
    # 匹配 "具体商品：xxx" 或 "具体商品: xxx"
    m = re.search(r"具体商品[：:]\s*(.+?)(?:\n|$)", output)
    if m:
        return m.group(1).strip()
    # fallback: 整段输出截断
    return output.strip().split("\n")[0][:80]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=0, help="只处理前 N 条 (0 = 全部)")
    ap.add_argument("--dry-run", action="store_true", help="只准备图片和 prompt, 不实际调 LLM")
    ap.add_argument("--download-only", action="store_true", help="只下载图片不调 LLM (调试用)")
    args = ap.parse_args()

    print("=== LLM 推测具体商品 ===")
    print()

    # 1. 拉全部笔记
    print("[1/4] 拉取笔记表...")
    notes = list_notes()
    print(f"      共 {len(notes)} 条")
    if args.max > 0:
        notes = notes[:args.max]
        print(f"      --max 限制: 只处理前 {len(notes)} 条")

    # 2. 下载并转 PNG
    print()
    print(f"[2/4] 下载图片到 {WORK_DIR}")
    tasks = []
    for i, n in enumerate(notes, 1):
        rid = n["record_id"]
        token = n["image_file_token"]
        if not token:
            print(f"  [{i}/{len(notes)}] {rid}: 无图片, 跳过")
            continue
        png_path = WORK_DIR / f"{rid}.png"
        if png_path.exists():
            print(f"  [{i}/{len(notes)}] {rid}: 已存在")
            tasks.append((n, png_path))
            continue
        print(f"  [{i}/{len(notes)}] {rid}: 下载 {n['image_name']}")
        time.sleep(PACING_API_SEC)
        ok, err = download_and_convert(rid, token, png_path)
        if ok:
            tasks.append((n, png_path))
        else:
            print(f"      FAIL: {err}")

    print(f"      成功准备 {len(tasks)} 张图片")
    if args.download_only:
        return

    # 3. 输出任务清单 (让上层 agent 按这个清单调 LLM)
    task_list = []
    for n, png_path in tasks:
        task_list.append({
            "record_id": n["record_id"],
            "title": n["title"],
            "desc": n["desc"],
            "tags": n["tags"],
            "image_path": str(png_path),
            "prompt": make_llm_prompt(n["title"], n["desc"], n["tags"]),
        })

    task_file = WORK_DIR / "llm_tasks.json"
    task_file.write_text(json.dumps(task_list, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"[3/4] 任务清单已写到: {task_file}")
    print(f"      共 {len(task_list)} 个任务, 每个含 image_path + prompt")
    if args.dry_run:
        print("      (--dry-run 模式: 不调 LLM)")
        return

    print()
    print("[4/4] 实际调用 LLM 需要 WorkBuddy 主对话执行 (本脚本退出)")
    print("      在 WorkBuddy 里按 llm_tasks.json 逐条调 mcp__zai-vision-mcp__analyze_image")
    print("      把结果汇总到 llm_results.json 后, 再跑 writeback_llm_results.py 写回飞书")


if __name__ == "__main__":
    main()
