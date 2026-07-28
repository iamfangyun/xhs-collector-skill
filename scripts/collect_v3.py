#!/usr/bin/env python
# -*- coding: utf-8 -*-
# xhs-collector v3 - 全 Edge 单浏览器架构（弃用 MCP）
#
# 核心改进（相对 v2）:
#   - 弃用 xiaohongshu-mcp 和它的 headless Chromium
#   - 全部采集通过 Playwright CDP 接管真实 Edge 完成
#   - 一个浏览器 = 一个登录态，不再有"Edge 正常但 MCP 过期"的故障
#   - 真实 Edge 指纹，比 headless Chromium 隐蔽得多
#
# 工作原理:
#   1. Playwright connect_over_cdp 接管已启动的 Edge（端口 9222）
#   2. 三重登录检查（cookie + 页���特征 + API 探测）
#   3. 搜 redId 拿 user_id + xsec_token（拦截 onebox API）
#   4. 导航主页拦截 user_posted API → 笔记列表
#   5. 逐篇导航笔记页拦截 feed_detail + comment/page API
#   6. 下载图片
#
# 用法:
#   python collect_v3.py --red-id 95466594071 --date 2026-07-26
#   python collect_v3.py --profile-url "https://...?xsec_token=..." --date 2026-07-26
#
# 前置条件:
#   Edge 已通过 ensure_edge.ps1 启动（端口 9222），已扫码登录小号
#
# 风控措施:
#   - log-normal 随机间隔（不是固定值，更像人类行为）
#   - 每次页面滚动后随机等待 2-5 秒
#   - 笔记之间间隔 30-60 秒（随机）
#   - 风控信号立即停止
#   - 每 5 篇笔记小休 60-120 秒
import argparse
import asyncio
import datetime
import json
import math
import random
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

CDP_ENDPOINT = "http://127.0.0.1:9222"
BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 风控信号关键词（只在响应的 code/msg 字段里匹配，不做全文匹配）
# 注意：很多关键词是正常字段子串（verify/captcha/login/异常），不能做全文匹配
# 正确做法：只检查 JSON 的 code 字段（非 0）和 msg 字段
RISK_CODE_PATTERNS = [
    -100,      # 请求过于频繁
    461,       # 操作过于频繁
    -102,      # 网络异常
    300012,    # 风控
    300013,    # 风控
    60001,     # 安全验证
]
RISK_MSG_KEYWORDS = [
    "风控", "请求过于频繁", "操作太频繁", "请稍后再试",
    "滑动验证", "安全验证", "访问被拒绝",
]


def log(msg):
    ts = datetime.datetime.now(BEIJING_TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_risk_text(text):
    """已废弃——改用 check_risk_data 检查结构化 JSON。保留向后兼容。"""
    pass  # 不再做全文匹配，太多误报


def check_risk_data(data):
    """检查结构化 JSON 响应是否含风控信号。
    只看 code 字段和 msg 字段，不做全文匹配（避免误报）。
    """
    if not isinstance(data, dict):
        return
    code = data.get("code", 0)
    msg = data.get("msg", "") or ""
    # 检查 code
    if code in RISK_CODE_PATTERNS:
        raise RuntimeError(f"⚠️ 风控信号: code={code}, msg={msg} — 立即停止")
    # 检查 msg
    for kw in RISK_MSG_KEYWORDS:
        if kw in msg:
            raise RuntimeError(f"⚠️ 风控信号: msg 含 '{kw}' (code={code}) — 立即停止")


def human_delay(min_sec=2.0, max_sec=5.0):
    return random.uniform(min_sec, max_sec)


def api_pacing_delay():
    """log-normal 分布，中位数 ~2.5s，钳制到 [1.5, 8.0]。"""
    delay = random.lognormvariate(math.log(2.5), 0.4)
    return min(max(delay, 1.5), 8.0)


def note_gap_delay():
    return random.uniform(30.0, 60.0)


def batch_rest_delay():
    return random.uniform(60.0, 120.0)


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name)[:50]


def in_date_range(ts_ms, target_date_str):
    if not ts_ms:
        return False, None
    # SSR 可能返回字符串型时间戳，确保转成数字
    try:
        ts_ms = int(ts_ms)
    except (ValueError, TypeError):
        return False, None
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, BEIJING_TZ)
    return dt.strftime("%Y-%m-%d") == target_date_str, dt


def download_image(url, save_path):
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


class XhsCollector:
    """全 Edge 单浏览器采集器。

    拦截器设计：一个中央 response handler（在 connect 时注册一次），
    按 URL 模式分发到不同的 capture buffer。避免多次注册导致监听器累积。
    """

    def __init__(self, target_date: str, out_root: Path):
        self.target_date = target_date
        self.out_root = out_root
        self.browser = None
        self.ctx = None
        self.page = None
        # 中央拦截缓冲：{pattern_name: [data, ...]}
        self._buffers = {}
        self._active_patterns = set()  # 当前激活的 pattern 名

    async def connect(self):
        log(f"[edge] 连接 CDP {CDP_ENDPOINT}...")
        try:
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            raise RuntimeError(
                f"无法连接 Edge CDP ({CDP_ENDPOINT}): {e}\n"
                f"请先运行 ensure_edge.ps1 启动 Edge 调试端口"
            )
        self.ctx = self.browser.contexts[0] if self.browser.contexts else None
        if not self.ctx:
            raise RuntimeError("Edge 没有 context")
        for p in self.ctx.pages:
            if "xiaohongshu.com" in p.url:
                self.page = p
                break
        if not self.page:
            self.page = await self.ctx.new_page()
        # 注册唯一一个中央 response handler
        self.ctx.on("response", lambda r: asyncio.create_task(self._on_response(r)))
        log(f"[edge] 已接管 Edge，当前页面: {self.page.url[:60]}")

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass

    # ===== 中央拦截器 =====

    def activate(self, *pattern_names):
        """激活一个或多个拦截 pattern，并返回清空后的缓冲区引用。
        每次 activate 会清空对应 pattern 的旧数据。
        """
        for name in pattern_names:
            self._buffers[name] = []
            self._active_patterns.add(name)

    def get_captured(self, pattern_name):
        """取出某个 pattern 的缓冲数据（不清空）。"""
        return self._buffers.get(pattern_name, [])

    def deactivate(self, *pattern_names):
        """停用 pattern（不再匹配）。"""
        for name in pattern_names:
            self._active_patterns.discard(name)

    async def _on_response(self, resp):
        """中央响应处理器。按 URL 分发到激活的 pattern buffer。"""
        if not self._active_patterns:
            return
        try:
            url = resp.url
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await resp.text()
            data = json.loads(body)
            check_risk_data(data)  # 只检查结构化字段，不做全文匹配

            # 分发到激活的 pattern
            if "onebox" in self._active_patterns and "search/onebox" in url:
                self._buffers.setdefault("onebox", []).append(data)
            if "profile" in self._active_patterns and (
                "/api/sns/web/v1/user/otherbeta" in url or "/api/sns/web/v1/user/info" in url
            ):
                self._buffers.setdefault("profile", []).append(data)
            if "posted" in self._active_patterns and "/api/sns/web/v1/user_posted" in url:
                self._buffers.setdefault("posted", []).append(data)
            if "detail" in self._active_patterns and (
                "/api/sns/web/v1/feed_detail" in url or "/api/sns/web/v1/feed" in url
            ):
                self._buffers.setdefault("detail", []).append(data)
            if "comment" in self._active_patterns and (
                "/api/sns/web/v2/comment/page" in url
                or "/api/sns/web/v1/comment/page" in url
                or "/api/sns/web/v2/comment/get_comments" in url
            ):
                self._buffers.setdefault("comment", []).append(data)
            if "userme" in self._active_patterns and (
                "/api/sns/web/v2/user/me" in url or "/api/sns/web/v1/user/me" in url
            ):
                self._buffers.setdefault("userme", []).append(data)
        except RuntimeError:
            raise  # 风控关键词向上抛
        except Exception:
            pass

    # ===== 登录检查（三重保险） =====

    async def check_login(self) -> bool:
        log("[login] 三重登录检查...")

        # 1. Cookie 检查
        cookies = await self.ctx.cookies()
        session = next((c for c in cookies if c["name"] == "web_session" and len(c["value"]) > 20), None)
        if not session:
            log("[login] ❌ web_session cookie 不存在或太短")
            return False
        log(f"[login] ① Cookie OK: web_session={session['value'][:16]}...")

        # 2. 页面特征检查
        await self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(3, 5))
        login_btn = await self.page.query_selector('.login-btn, [class*="login-container"], .side-bar .login-btn')
        if login_btn:
            log("[login] ❌ 页面检测到登录按钮，未登录")
            return False
        if "/login" in self.page.url or "login" in self.page.url.split("?")[0]:
            log(f"[login] ❌ URL 被重定向到登录页: {self.page.url}")
            return False
        log("[login] ② 页面特征 OK：无登录按钮，未重定向")

        # 3. API 探测：用 user/me API 判断登录态（比 homefeed 更可靠）
        self.activate("userme")
        await self.page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(4, 6))
        userme_hits = self.get_captured("userme")
        self.deactivate("userme")

        if not userme_hits:
            log("[login] ❌ 未拦截到 user/me API，可能登录态无效")
            return False
        # 检查 user/me 是否返回了有效用户数据
        me_data = userme_hits[0].get("data", {})
        me_success = userme_hits[0].get("success", False)
        me_uid = me_data.get("user_id", "")
        me_nick = me_data.get("nickname", "")
        me_guest = me_data.get("guest", False)  # 关键：guest:true = 游客态，不是真登录
        if not me_success or not me_uid:
            log(f"[login] ❌ user/me 返回无效: success={me_success}, user_id={me_uid}")
            return False
        if me_guest:
            log(f"[login] ❌ user/me 返回 guest:true — cookie 虽然存在，但服务端判定为游客态（session 失效或未真正登录）")
            log(f"[login]    guest uid={me_uid}，这不是真实登录账号。需要重新扫码登录。")
            return False
        log(f"[login] ③ API 探测 OK：user/me 返回 {me_nick} (uid={me_uid[:12]}..., guest={me_guest})")
        # 记录基线 session 值，供采集中途 quick_session_check 比对
        self._baseline_session = session["value"]
        log("[login] ✅ 三重检查全过，已记录 session 基线")
        return True

    # ===== 采集中途登录态复检（防止 session 中途失效后盲目请求）=====
    # 7-28 风控事件的核心教训：check_login 只在开头做一次，session 中途失效后
    # 代码会继续用失效 cookie 密集请求，触发"异常状态被标记"。
    # 这里加两层复检：
    #   1. quick_session_check: 每篇笔记前调用，只看 cookie，无网络请求
    #   2. periodic_login_probe: 每 N 篇调用一次，发 user/me API 探测 guest

    async def quick_session_check(self) -> bool:
        """轻量 cookie 复检（无网络请求）。
        检查 web_session cookie 是否还在、是否变化。
        session 中途消失或变空，基本可以判定已登出。
        """
        try:
            cookies = await self.ctx.cookies()
            session = next((c for c in cookies if c["name"] == "web_session"), None)
            if not session or len(session.get("value", "")) < 20:
                log("[session] ❌ web_session 消失或变短 — session 可能已失效")
                return False
            # 检查 cookie 是否和登录检查时记录的一致（变了说明被服务端刷新/清空）
            current_val = session["value"]
            if hasattr(self, "_baseline_session") and self._baseline_session:
                if current_val != self._baseline_session:
                    log("[session] ⚠️ web_session 值发生变化（可能被服务端刷新）")
                    log(f"[session]   旧: {self._baseline_session[:16]}... → 新: {current_val[:16]}...")
                    # 不直接判失败：服务端正常续期也会换值。更新基线，靠后续 probe 确认。
                    self._baseline_session = current_val
            return True
        except Exception as e:
            log(f"[session] cookie 检查异常: {e}")
            return False

    async def periodic_login_probe(self) -> bool:
        """周期性 user/me 探测（每 N 篇调用一次）。
        发真实 API 请求，检查 guest:false。
        比 quick_session_check 慢，但能查出"cookie 在但服务端已判游客"的情况。
        """
        log("[probe] 周期性登录态探测 (user/me)...")
        self.activate("userme")
        try:
            await self.page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(3, 5))
            # reload 触发 user/me
            await self.page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(human_delay(3, 5))
            userme_hits = self.get_captured("userme")
        finally:
            self.deactivate("userme")

        if not userme_hits:
            log("[probe] ❌ 未拦截到 user/me 响应 — 可能登录态已失效或被风控")
            return False
        me_data = userme_hits[0].get("data", {})
        me_guest = me_data.get("guest", False)
        me_success = userme_hits[0].get("success", False)
        me_uid = me_data.get("user_id", "")
        if not me_success or not me_uid or me_guest:
            log(f"[probe] ❌ 登录态已失效: success={me_success}, uid={me_uid}, guest={me_guest}")
            log("[probe]   cookie 可能还在，但服务端已判定为游客态。必须停止采集。")
            return False
        log(f"[probe] ✅ 登录态正常: {me_data.get('nickname','?')} (guest={me_guest})")
        return True

    # ===== 刷新 token =====

    async def refresh_token(self, red_id: str) -> dict:
        log(f"[token] 搜索 redId={red_id} 刷新 token...")
        self.activate("onebox")

        await asyncio.sleep(human_delay(3, 5))
        search_url = f"https://www.xiaohongshu.com/search_result?keyword={red_id}&source=web_explore_feed&type=51"
        log(f"[token] 导航搜索页: {search_url[:70]}...")
        await self.page.goto(search_url, wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(10, 14))

        onebox_hits = self.get_captured("onebox")
        self.deactivate("onebox")

        # 匹配目标 redId
        matched_uob = None
        for data in onebox_hits:
            items = data.get("data", {}).get("onebox_list", [])
            for item in items:
                uob = item.get("user_one_box")
                if uob and (str(uob.get("red_id", "")) == red_id or str(uob.get("red_id", "")) == red_id.lstrip("0")):
                    matched_uob = uob
                    break
            if matched_uob:
                break

        if not matched_uob:
            # 备用：DOM 提取
            log("[token] onebox 未命中，尝试 DOM...")
            links = await self.page.evaluate("""(redId) => {
                const out = [];
                document.querySelectorAll('a[href*="/user/profile/"]').forEach(a => {
                    const text = (a.textContent || '').trim();
                    if (text.includes(redId)) {
                        out.push({ href: a.href, text: text.slice(0, 200) });
                    }
                });
                return out;
            }""", red_id)
            if links:
                link = links[0]["href"]
                parsed = urllib.parse.urlparse(link)
                qs = urllib.parse.parse_qs(parsed.query)
                user_id = parsed.path.split("/")[-1]
                xsec_token = urllib.parse.unquote(qs.get("xsec_token", [""])[0])
                return {
                    "user_id": user_id, "xsec_token": xsec_token,
                    "nickname": "", "fans": "", "note_count": 0,
                    "red_id": red_id, "source": "dom",
                }

        if not matched_uob:
            raise RuntimeError(f"未找到 redId={red_id} 的账号信息")

        xsec_token = matched_uob.get("xsec_token", "")
        user_id = matched_uob.get("id", "")
        encoded = urllib.parse.quote(xsec_token, safe="")
        profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token={encoded}&xsec_source=pc_search"

        result = {
            "user_id": user_id,
            "xsec_token": xsec_token,
            "nickname": matched_uob.get("title", ""),
            "fans": matched_uob.get("fans", ""),
            "note_count": matched_uob.get("note_count", 0),
            "red_id": red_id,
            "profile_url": profile_url,
            "avatar": matched_uob.get("image", ""),
        }
        log(f"[token] ✅ {result['nickname']} (fans={result['fans']}, token={xsec_token[:16]}...)")
        return result

    # ===== 采集账号主页信息 =====
    # 注意：小红书 profile 页是 SSR（服务端渲染），用户数据/笔记列表直接嵌在 HTML 里，
    # 不发 user/otherbeta / user_posted API。所以这里改用 DOM 提取，不再依赖 API 拦截。
    # 滚动加载更多笔记时，新笔记也是 SSR 进 DOM，继续从 DOM 读。

    async def _extract_profile_from_dom(self, profile_url: str) -> tuple:
        """从 profile 页 DOM 提取账号��息 + 笔记列表。"""
        dom = await self.page.evaluate("""() => {
            const out = {};
            // 账号信息
            const nameEl = document.querySelector('.user-name, .nickname');
            out.nickname = nameEl ? nameEl.textContent.trim() : '';
            const descEl = document.querySelector('.user-desc, .desc');
            out.desc = descEl ? descEl.textContent.trim() : '';
            // 互动数据：通常顺序是 [关注, 粉丝, 获赞与收藏]
            const countEls = document.querySelectorAll('.user-interactions .count, .user-interaction .count, [class*="interact"] .num, .fans-count .count');
            out.interactionCounts = Array.from(countEls).map(e => e.textContent.trim());
            // 头像
            const avatarEl = document.querySelector('.user-avatar img, .avatar img, [class*="user-avatar"] img');
            out.avatar = avatarEl ? (avatarEl.src || avatarEl.dataset.src || '') : '';
            // IP 归属地（如果有）
            const ipEl = document.querySelector('.ip-location, [class*="ip-location"], .location');
            out.ipLocation = ipEl ? ipEl.textContent.trim() : '';
            // 笔记卡片
            const noteCards = document.querySelectorAll('section.note-item, [class*="note-item"]');
            out.notes = Array.from(noteCards).map(card => {
                // profile 页笔记链接格式有两种：
                //   1. /explore/{note_id}                    （隐藏链接，无 token）
                //   2. /user/profile/{user_id}/{note_id}?xsec_token=xxx  （可见的封面/标题链接，带 token）
                // 优先取带 xsec_token 的 profile 链接（格式 2），它有笔记详情所需的 token
                const profileLink = card.querySelector('a[href*="/user/profile/"][href*="xsec_token"]');
                const exploreLink = card.querySelector('a[href*="/explore/"], a[href*="/discovery/item/"]');
                const titleEl = card.querySelector('.title, [class*="title"], .footer .title');
                const coverEl = card.querySelector('img');
                let noteId = '';
                let noteXsecToken = '';
                let href = '';
                if (profileLink && profileLink.href) {
                    href = profileLink.href;
                    // /user/profile/{user_id}/{note_id}?xsec_token=xxx → 取路径最后一段
                    const idMatch = href.match(/\/([a-f0-9]{24})(?:\?|$)/);
                    if (idMatch) noteId = idMatch[1];
                } else if (exploreLink && exploreLink.href) {
                    href = exploreLink.href;
                    // /explore/{note_id} 或 /discovery/item/{note_id}
                    const idMatch = href.match(/\/(?:explore|discovery\/item)\/([a-f0-9]+)/);
                    if (idMatch) noteId = idMatch[1];
                }
                const tokenMatch = href.match(/xsec_token=([^&]+)/);
                if (tokenMatch) noteXsecToken = decodeURIComponent(tokenMatch[1]);
                return {
                    noteId: noteId,
                    xsecToken: noteXsecToken,
                    title: titleEl ? titleEl.textContent.trim() : '',
                    cover: coverEl ? (coverEl.src || coverEl.dataset.src || '') : '',
                    href: href,
                };
            }).filter(n => n.noteId);  // 只要能解析出 note_id 的
            return out;
        }""")

        # 解析互动数据
        counts = dom.get("interactionCounts", [])
        def parse_count(s):
            s = s.replace(',', '').replace(' ', '')
            if '万' in s:
                try: return int(float(s.replace('万', '')) * 10000)
                except: return 0
            try: return int(s)
            except: return 0

        follows = parse_count(counts[0]) if len(counts) > 0 else 0
        fans = parse_count(counts[1]) if len(counts) > 1 else 0
        interaction = parse_count(counts[2]) if len(counts) > 2 else 0

        account = {
            "redId": "",  # DOM 里通常不直接暴露 redId，后面从 token 信息补
            "user_id": "",
            "nickname": dom.get("nickname", ""),
            "desc": dom.get("desc", ""),
            "ip_location": dom.get("ipLocation", ""),
            "avatar": dom.get("avatar", ""),
            "interactions": {
                "follows": follows,
                "fans": fans,
                "interaction": interaction,
            },
            "profile_url_with_token": profile_url,
        }
        return account, dom.get("notes", [])

    async def fetch_profile(self, user_id: str, xsec_token: str, red_id: str = "") -> tuple:
        log(f"[profile] 导航主页 user_id={user_id}...")
        encoded = urllib.parse.quote(xsec_token, safe="")
        profile_url = f"https://www.xiaohongshu.com/user/profile/{user_id}?xsec_token={encoded}&xsec_source=pc_search"

        await asyncio.sleep(api_pacing_delay())
        await self.page.goto(profile_url, wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(6, 9))

        account, notes = await self._extract_profile_from_dom(profile_url)
        account["user_id"] = user_id
        account["redId"] = red_id  # 从 token 信息补

        if not account["nickname"]:
            raise RuntimeError("profile 页 DOM 未提取到用户名，可能页面未正常渲染")

        log(f"[profile] ✅ {account['nickname']} (fans={account['interactions']['fans']}, 首页笔记 {len(notes)} 篇)")
        return account, notes

    async def scroll_for_notes(self, max_scrolls: int = 15, known_notes: list = None) -> list:
        """滚动加载更多笔记。SSR 架构下，新笔记也是进 DOM，从 DOM 提取。"""
        log(f"[scroll] 开始滚动加载（最多 {max_scrolls} 次）...")
        known_ids = set()
        if known_notes:
            for n in known_notes:
                nid = n.get("noteId") or n.get("note_id") or n.get("id")
                if nid:
                    known_ids.add(nid)

        all_notes = list(known_notes) if known_notes else []

        for i in range(max_scrolls):
            await asyncio.sleep(human_delay(2.5, 5.0))
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(human_delay(3.0, 5.5))

            # 每次滚动后从 DOM 重新提取所有笔记
            _, current_notes = await self._extract_profile_from_dom(self.page.url)
            new_count = 0
            current_ids = set()
            for n in current_notes:
                nid = n.get("noteId")
                current_ids.add(nid)
                if nid and nid not in known_ids:
                    known_ids.add(nid)
                    all_notes.append(n)
                    new_count += 1

            log(f"  [scroll] 第 {i+1}/{max_scrolls} 次，DOM 笔记 {len(current_notes)} 篇，新增 {new_count}")
            if new_count == 0 and i > 0:
                log(f"  [scroll] 无新增，停止滚动")
                break

        # 转换为旧 schema 兼容格式
        normalized = []
        for n in all_notes:
            normalized.append({
                "note_id": n.get("noteId", ""),
                "xsec_token": n.get("xsecToken", ""),
                "display_title": n.get("title", ""),
                "cover": n.get("cover", ""),
            })
        log(f"[scroll] 完成，共 {len(normalized)} 条笔记元数据")
        return normalized

    # ===== 采集笔记详情 + 评论 =====
    # 笔记详情页是混合渲染：
    #   - 笔记本身是 SSR，数据在 __INITIAL_STATE__.note.noteDetailMap[noteId].note 里
    #   - 评论通过 /api/sns/web/v2/comment/page API 异步加载（可拦截）

    async def fetch_note_detail(self, note_id: str, xsec_token: str) -> dict:
        encoded = urllib.parse.quote(xsec_token, safe="")
        note_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={encoded}&xsec_source=pc_search"

        self.activate("comment")  # 只需要拦评论 API（详情是 SSR）
        await self.page.goto(note_url, wait_until="domcontentloaded")
        await asyncio.sleep(human_delay(6, 10))
        # 滚动加载评论
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(human_delay(3, 5))
        self.deactivate("comment")

        # 从 noteDetailMap 提取笔记详情（SSR）
        note = await self.page.evaluate("""(noteId) => {
            try {
                const ndm = window.__INITIAL_STATE__?.note?.noteDetailMap;
                if (!ndm || !ndm[noteId]) return null;
                const entry = ndm[noteId];
                // Vue3 ref：实际数据在 _rawValue 或 _value 里
                const raw = entry?._rawValue !== undefined ? entry._rawValue : entry;
                const note = raw?.note || raw;
                if (!note) return null;
                // 深拷贝需要的字段（避免 circular structure）
                return {
                    noteId: note.noteId || note.note_id,
                    title: note.title || note.display_title || '',
                    desc: note.desc || '',
                    type: note.type || '',
                    time: note.time || note.timestamp || null,
                    ipLocation: note.ipLocation || note.ip_location || '',
                    xsecToken: note.xsecToken || note.xsec_token || '',
                    interactInfo: {
                        likedCount: note.interactInfo?.likedCount ?? note.interact_info?.liked_count,
                        collectedCount: note.interactInfo?.collectedCount ?? note.interact_info?.collected_count,
                        commentCount: note.interactInfo?.commentCount ?? note.interact_info?.comment_count,
                        shareCount: note.interactInfo?.shareCount ?? note.interact_info?.share_count,
                    },
                    imageList: (note.imageList || note.image_list || []).map(img => {
                        if (typeof img === 'string') return { urlDefault: img };
                        return {
                            urlDefault: img?.urlDefault || img?.url_default || img?.url || '',
                            width: img?.width,
                            height: img?.height,
                        };
                    }),
                    user: {
                        userId: note.user?.userId || note.user?.user_id || '',
                        nickname: note.user?.nickname || '',
                        avatar: note.user?.avatar || '',
                    },
                    tagList: (note.tagList || note.tag_list || []).map(t => ({
                        id: t?.id || t?.tagId,
                        name: t?.name || t?.tagName || '',
                        type: t?.type,
                    })),
                };
            } catch (e) {
                return { _error: e.message };
            }
        }""", note_id)

        if not note or note.get("_error"):
            err = note.get("_error") if note else "noteDetailMap 里没有这个 noteId"
            raise RuntimeError(f"笔记详情提取失败 {note_id}: {err}")

        # 评论（API 拦截）
        comments_data = self.get_captured("comment")
        all_comments = []
        has_more = False
        for batch in comments_data:
            d = batch.get("data", {}) if isinstance(batch, dict) else {}
            cs = d.get("comments", []) or batch.get("comments", [])
            all_comments.extend(cs)
            if d.get("has_more"):
                has_more = True

        return {
            "note": note,
            "comments": {
                "list": all_comments,
                "count": len(all_comments),
                "has_more": has_more,
            },
        }


async def run(args):
    out_root = Path(args.out).absolute()
    out_root.mkdir(parents=True, exist_ok=True)

    collector = XhsCollector(args.date, out_root)
    try:
        await collector.connect()

        # 1. 登录检查
        log("[1/7] 登录检查...")
        if not await collector.check_login():
            log("❌ 未登录或登录态失效，请重启 Edge 并重新扫码登录小号")
            sys.exit(1)

        # 2. 拿 user_id + xsec_token
        log("[2/7] 获取 user_id + xsec_token...")
        red_id_for_profile = ""
        if args.red_id:
            token_info = await collector.refresh_token(args.red_id)
            user_id = token_info["user_id"]
            xsec_token = token_info["xsec_token"]
            red_id_for_profile = args.red_id
        else:
            parsed = urllib.parse.urlparse(args.profile_url)
            m = re.match(r"^/user/profile/([a-f0-9]{24})$", parsed.path)
            if not m:
                raise RuntimeError(f"URL 路径无法解析 user_id: {parsed.path}")
            user_id = m.group(1)
            qs = urllib.parse.parse_qs(parsed.query)
            xsec_token = urllib.parse.unquote(qs.get("xsec_token", [""])[0])
            if not xsec_token:
                raise RuntimeError("URL 里没有 xsec_token")

        # 3. 采集账号信息 + 首页笔记列表（DOM 提取，SSR 架构）
        log("[3/7] 采集账号信息...")
        account, first_notes = await collector.fetch_profile(user_id, xsec_token, red_id=red_id_for_profile)

        # 4. 滚动加载更多笔记（SSR，从 DOM 累积）
        log("[4/7] 滚动加载笔记列表...")
        more_notes = await collector.scroll_for_notes(max_scrolls=args.max_scrolls, known_notes=first_notes)

        # 合并 + 去重（first_notes 已在 scroll_for_notes 里作为 base）
        all_notes_meta = {}
        for n in first_notes + more_notes:
            nid = n.get("note_id") or n.get("id") or n.get("noteId")
            if nid and nid not in all_notes_meta:
                all_notes_meta[nid] = n
        notes_list = list(all_notes_meta.values())
        log(f"[4/7] 合并去重后 {len(notes_list)} 篇笔记")

        # 5. 逐篇拉详情，按日期过滤
        log(f"[5/7] 逐篇拉详情过滤日期 {args.date}...")
        matched = []
        target_date_obj = datetime.date.fromisoformat(args.date)

        # 熔断参数：连续 N 次错误立即停，避免 session 失效后撞墙
        CONSECUTIVE_ERROR_LIMIT = 3
        consecutive_errors = 0
        # 周期性探测：每 N 篇做一次 user/me 探测（和小休点对齐，减少额外请求）
        PROBE_EVERY_N = 5

        for i, nm in enumerate(notes_list):
            note_id = nm.get("note_id") or nm.get("id") or nm.get("noteId")
            # 每篇笔记有自己的 xsec_token（从 profile 页 DOM 提取的），优先用它
            note_token = nm.get("xsec_token") or nm.get("xsecToken") or xsec_token
            if not note_id:
                continue

            # ===== 采集中途登录态复检（P0-1）=====
            # 每篇开始前：轻量 cookie 检查（无请求，毫秒级）
            if not await collector.quick_session_check():
                log(f"  ⚠️⚠️ [{i}] session 复检失败 — cookie 已失效，立即停止采集避免被风控")
                sys.exit(3)
            # 每 N 篇：深度 user/me 探测（查 guest，发真实请求）
            if i > 0 and i % PROBE_EVERY_N == 0:
                if not await collector.periodic_login_probe():
                    log(f"  ⚠️⚠️ [{i}] 周期性探测判定登录态已失效 — 立即停止采集")
                    log(f"       这是 7-28 风控事件的核心防御：cookie 可能还在，")
                    log(f"       但服务端已判游客态，继续请求会触发警告。")
                    sys.exit(3)

            try:
                await asyncio.sleep(api_pacing_delay())
                detail = await collector.fetch_note_detail(note_id, note_token)
                note = detail["note"]
                ts_ms = note.get("time") or note.get("timestamp")
                if not ts_ms:
                    log(f"  [{i}] {note_id} 无 time 字段，跳过")
                    continue

                hit, dt = in_date_range(ts_ms, args.date)
                title = (note.get("title") or note.get("display_title") or "")[:30]

                # 成功处理一篇，重置错误计数
                consecutive_errors = 0

                if hit:
                    log(f"  [{i}] ✅ {dt.strftime('%H:%M')} {note_id} {title}")
                    matched.append({"note_meta": nm, "detail": detail, "dt": dt})
                    pause = note_gap_delay()
                    log(f"      命中，随机休眠 {pause:.0f}s")
                    await asyncio.sleep(pause)
                else:
                    # 如果时间早于目标日期 7 天以上，提前结束（笔记按时间倒序）
                    if dt and dt.date() < target_date_obj - datetime.timedelta(days=7):
                        log(f"  [{i}] ⏹️ {dt.strftime('%m-%d')} 已超出 7 天范围，停止")
                        break
                    log(f"  [{i}] ❌ {dt.strftime('%m-%d %H:%M')} {note_id} {title}")

                if (i + 1) % PROBE_EVERY_N == 0:
                    rest = batch_rest_delay()
                    log(f"      第 {i+1} 篇，小休 {rest:.0f}s")
                    await asyncio.sleep(rest)

            except Exception as e:
                err = str(e)
                if "风控" in err:
                    log(f"  ⚠️⚠️⚠️ 疑似风控，立即终止！{err}")
                    sys.exit(3)
                # P0-2: 连续错误熔断
                consecutive_errors += 1
                log(f"  [{i}] ERROR {note_id}: {type(e).__name__}: {err[:150]} (连续第 {consecutive_errors}/{CONSECUTIVE_ERROR_LIMIT} 次)")
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    log(f"  ⚠️⚠️⚠️ 连续 {CONSECUTIVE_ERROR_LIMIT} 次错误，判定 session 失效或被风控，立即终止！")
                    log(f"       这可能是 cookie 过期、服务端 401、或风控降级的早期信号。")
                    log(f"       继续请求会重蹈 7-28 事件覆辙。已保存 {len(matched)} 篇有效数据。")
                    sys.exit(3)
                # 连续错误可能是风控前兆，多等一会
                await asyncio.sleep(api_pacing_delay() * 2)
                continue  # 跳过这篇，继续下一篇
            except BaseException as be:
                # asyncio.CancelledError 等不继承 Exception 的异常
                if isinstance(be, SystemExit):
                    raise
                consecutive_errors += 1
                log(f"  [{i}] BASE-ERROR {note_id}: {type(be).__name__}: {str(be)[:150]} (连续第 {consecutive_errors}/{CONSECUTIVE_ERROR_LIMIT} 次)")
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    log(f"  ⚠️⚠️⚠️ 连续 {CONSECUTIVE_ERROR_LIMIT} 次 BaseException，立即终止！")
                    sys.exit(3)
                await asyncio.sleep(api_pacing_delay() * 2)
                continue

        log(f"\n  命中 {len(matched)} 篇")

        # 6. 整理 + 下载图片
        log("[6/7] 整理结果 + 下载图片...")
        red_id = account["redId"] or args.red_id or user_id[:8]
        nickname = account["nickname"] or "unknown"
        safe_nick = safe_filename(nickname)
        out_dir = out_root / f"{red_id}_{safe_nick}"
        img_dir = out_dir / "images"

        notes_out = []
        for m in matched:
            note = m["detail"]["note"]
            comments = m["detail"]["comments"]

            img_files = []
            image_list = note.get("imageList") or note.get("image_list") or []
            note_id_str = m["note_meta"].get("note_id") or m["note_meta"].get("noteId") or m["note_meta"].get("id") or ""
            for idx, img in enumerate(image_list):
                if isinstance(img, dict):
                    url = img.get("urlDefault") or img.get("url_default") or img.get("url") or ""
                else:
                    url = str(img)
                if url:
                    fname = f"{note_id_str}_{idx}.webp"
                    if download_image(url, img_dir / fname):
                        img_files.append(fname)
                    await asyncio.sleep(0.5)

            notes_out.append({
                "note_id": note.get("noteId") or note.get("note_id"),
                "xsec_token": note.get("xsecToken") or m["note_meta"].get("xsec_token", xsec_token),
                "type": note.get("type"),
                "title": note.get("title") or note.get("display_title") or "",
                "desc": note.get("desc") or "",
                "time_ms": note.get("time") or note.get("timestamp"),
                "time_str": m["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                "ip_location": note.get("ipLocation") or note.get("ip_location") or "",
                "interactions": note.get("interactInfo") or note.get("interact_info") or {},
                "image_files": img_files,
                "comments": {
                    "count": comments["count"],
                    "has_more": comments["has_more"],
                    "list": comments["list"],
                },
            })

        # 7. 输出 JSON
        log("[7/7] 输出 JSON...")
        result = {
            "collected_at": datetime.datetime.now(BEIJING_TZ).isoformat(),
            "target_date": args.date,
            "account": account,
            "matched_notes": notes_out,
            "collector_version": "v3-edge",
        }
        out_file = out_dir / f"{red_id}_{safe_nick}_{args.date}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        log("=" * 60)
        log("✅ 采集完成")
        log(f"   账号: {nickname} (redId={red_id})")
        log(f"   日期: {args.date}")
        log(f"   笔记: {len(notes_out)} 篇")
        log(f"   图片: {sum(len(n['image_files']) for n in notes_out)} 张")
        log(f"   评论: {sum(n['comments']['count'] for n in notes_out)} 条")
        log(f"   输出: {out_file}")
        log("=" * 60)

    finally:
        await collector.close()


def main():
    ap = argparse.ArgumentParser(description="xhs-collector v3 - 全 Edge 单浏览器架构")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--red-id", help="小红书账号 redId（纯数字），会自动刷新 token")
    group.add_argument("--profile-url", help="完整主页 URL（必须带 xsec_token）")
    ap.add_argument("--date", required=True, help="目标日期 YYYY-MM-DD（北京时间）")
    ap.add_argument("--out", default="./xhs_output", help="输出根目录")
    ap.add_argument("--max-scrolls", type=int, default=15, help="最大滚动次数（默认 15）")
    args = ap.parse_args()

    try:
        asyncio.run(run(args))
    except SystemExit:
        raise  # sys.exit 直接传递
    except Exception as e:
        import traceback
        log(f"❌ 未预期的异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
