#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# refresh_token.py - 小红书 redId 转 xsec_token 自动获取脚本
#
# 原理：
# - 通过 CDP 接管已启动的 Edge 浏览器（带调试端口 9222）
# - 用 Edge 里已登录的小土豆炒股账号搜索 redId
# - 从拦截到的 onebox API 响应中提取 user_id + xsec_token
#
# 前置条件：
# 1. Edge 已通过以下命令启动（独立实例，不影响日常 Chrome/Edge）
# 2. 临时 profile 已扫码登录小土豆炒股（首次启动需要，之后 cookies 自动持久化）
#
# 用法：
#   python refresh_token.py <redId>
#   python refresh_token.py <redId> --output tokens.json
#   python refresh_token.py <redId> --warmup-wait 5 --search-wait 12
import argparse
import asyncio
import json
import sys
import urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright

CDP_ENDPOINT = "http://127.0.0.1:9222"


async def refresh_token(red_id: str, warmup_wait: int = 5, search_wait: int = 12) -> dict:
    """根据 redId 通过 CDP 搜索获取最新的 user_id + xsec_token"""

    print(f"[refresh] redId={red_id}, warmup={warmup_wait}s, search={search_wait}s")

    async with async_playwright() as p:
        # 1. CDP 接管 Edge
        try:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            print(f"[refresh] 无法连接 Edge CDP ({CDP_ENDPOINT}): {e}")
            print(f"[refresh] 请先启动 Edge（独立实例）：")
            print(f"  msedge.exe --remote-debugging-port=9222 \\")
            print(f"    --user-data-dir=<临时目录> \\")
            print(f"    --no-first-run --no-default-browser-check \\")
            print(f"    https://www.xiaohongshu.com/explore")
            sys.exit(2)

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        if not ctx:
            print("[refresh] ❌ 没有 context")
            sys.exit(3)

        # 2. 拦截 onebox API 响应（核心数据源）
        onebox_data = []

        async def on_resp(resp):
            url = resp.url
            # onebox API 是"快捷搜索结果"，含用户信息
            if "search/onebox" not in url:
                return
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                body = await resp.text()
                data = json.loads(body)
                # 找含 user_one_box 的响应
                items = data.get("data", {}).get("onebox_list", [])
                for item in items:
                    uob = item.get("user_one_box")
                    if uob and uob.get("red_id") == red_id:
                        onebox_data.append(uob)
            except Exception:
                pass

        ctx.on("response", lambda r: asyncio.create_task(on_resp(r)))

        # 3. 找或新建小红书页面
        xhs_page = None
        for page in ctx.pages:
            if "xiaohongshu.com" in page.url:
                xhs_page = page
                break
        if not xhs_page:
            xhs_page = await ctx.new_page()

        # 4. 暖机：访问 explore 确认登录态
        print(f"[refresh] 暖机：访问 explore...")
        await xhs_page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        await asyncio.sleep(warmup_wait)

        # 检查登录态
        cookies = await ctx.cookies()
        session = next((c for c in cookies if c["name"] == "web_session" and len(c["value"]) > 20), None)
        if not session:
            print(f"[refresh] ⚠️ web_session 不存在，可能未登录。请在新 Edge 窗口扫码登录小土豆炒股。")
            sys.exit(4)
        print(f"[refresh] 登录态 OK: web_session={session['value'][:20]}...")

        # 5. 慢节奏：暖机后间隔 10s 再搜索（避免风控）
        print(f"[refresh] 等 10s（避免风控）...")
        await asyncio.sleep(10)

        # 6. 访问搜索页
        search_url = f"https://www.xiaohongshu.com/search_result?keyword={red_id}&source=web_explore_feed&type=51"
        print(f"[refresh] 搜索: {search_url}")
        await xhs_page.goto(search_url, wait_until="domcontentloaded")
        print(f"[refresh] 等 {search_wait}s 让搜索 API 完成...")
        await asyncio.sleep(search_wait)

        # 7. 优先用拦截到的 API 数据（最可靠）
        result = None
        if onebox_data:
            uob = onebox_data[0]
            result = _build_result(red_id, uob)

        # 8. 备用：从 DOM 提取（如果 API 拦截失败）
        if not result:
            print(f"[refresh] API 拦截未命中，尝试从 DOM 提取...")
            result = await _extract_from_dom(xhs_page, red_id)

        # 断开（不关闭浏览器，保持 Edge 运行）
        await browser.close()

        if not result:
            print(f"[refresh] ❌ 未能找到 redId={red_id} 的账号信息")
            sys.exit(5)

        print(f"[refresh] ✅ 成功获取:")
        print(f"  user_id   = {result['user_id']}")
        print(f"  xsec_token = {result['xsec_token']}")
        print(f"  nickname   = {result['nickname']}")
        print(f"  fans       = {result['fans']}")
        print(f"  note_count = {result['note_count']}")
        return result


def _build_result(red_id: str, uob: dict) -> dict:
    """从 onebox user_one_box 构建标准输出"""
    xsec_token = uob.get("xsec_token", "")
    user_id = uob.get("id", "")
    # 完整主页 URL（含 token，可直接喂给 collect.py）
    encoded_token = urllib.parse.quote(xsec_token, safe="")
    profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token={encoded_token}&xsec_source=pc_search"

    return {
        "red_id": red_id,
        "user_id": user_id,
        "xsec_token": xsec_token,
        "nickname": uob.get("title", ""),
        "fans": uob.get("fans", ""),
        "note_count": uob.get("note_count", 0),
        "update_time": uob.get("update_time", ""),
        "profile_url": profile_url,
        "avatar": uob.get("image", ""),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


async def _extract_from_dom(page, red_id: str):
    """备用：从 DOM 里找匹配 red_id 的 user/profile 链接"""
    print(f"[refresh] 从 DOM 提取（备用）...")
    links = await page.evaluate("""(redId) => {
        const out = [];
        document.querySelectorAll('a[href*="/user/profile/"]').forEach(a => {
            const text = (a.textContent || '').trim();
            // 文本里包含目标 redId（小红书号）才算
            if (text.includes(redId)) {
                out.push({ href: a.href, text: text.slice(0, 200) });
            }
        });
        return out;
    }""", red_id)

    if not links:
        return None

    # 第一个匹配的链接
    link = links[0]["href"]
    parsed = urllib.parse.urlparse(link)
    qs = urllib.parse.parse_qs(parsed.query)
    xsec_token = qs.get("xsec_token", [""])[0]
    user_id_match = urllib.parse.urlparse(link).path.split("/")[-1]

    return {
        "red_id": red_id,
        "user_id": user_id_match,
        "xsec_token": xsec_token,
        "nickname": "",
        "fans": "",
        "note_count": 0,
        "update_time": "",
        "profile_url": link,
        "avatar": "",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "dom",
    }


def main():
    parser = argparse.ArgumentParser(description="redId → xsec_token 自动获取")
    parser.add_argument("red_id", help="目标账号的小红书号（redId）")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径（不指定则只打印）")
    parser.add_argument("--warmup-wait", type=int, default=5, help="暖机等待秒数（默认 5）")
    parser.add_argument("--search-wait", type=int, default=12, help="搜索后等待秒数（默认 12，让 API 完成）")
    args = parser.parse_args()

    result = asyncio.run(refresh_token(
        args.red_id,
        warmup_wait=args.warmup_wait,
        search_wait=args.search_wait,
    ))

    output_json = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"\n[refresh] 已写入 {args.output}")
    else:
        print(f"\n[refresh] 结果:")
        print(output_json)


if __name__ == "__main__":
    main()
