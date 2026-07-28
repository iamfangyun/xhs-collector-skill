#!/usr/bin/env python
# -*- coding: utf-8 -*-
# xhs-collector 主采集脚本 (v2.1 - 适配 xiaohongshu-mcp v2.0.0+)
#
# 用法 (两种二选一):
#   A. 直接给 profile URL (已经带 xsec_token):
#      python collect.py --profile-url "https://www.xiaohongshu.com/user/profile/XXXX?xsec_token=YYYY" --date 2026-07-26
#
#   B. 只给 redId (token 自动刷新, 需要本地 Edge 已经用 9222 调试端口启动并扫码登录):
#      python collect.py --red-id 95466594071 --date 2026-07-26
#
# v2.1 新增 (2026-07-27):
#   - --red-id 参数: 当用户只提供 redId 时, 自动调用 refresh_token.py 通过 CDP+Edge 刷新 token
#   - --profile-url 仍然支持, 向后兼容
#   - 新增 --refresh-script 参数可指定 refresh_token.py 的路径 (默认同目录)
#
# v2.0 变化:
#   - 输入参数: --red-id → --profile-url (完整主页URL, 必须带 xsec_token)
#   - 原因: xiaohongshu-mcp v2.0.0 的 user_profile 强制要求 user_id(24位hex) + xsec_token
#           redId(8-11位数字) 既不是 user_id 也无法换取 xsec_token
#   - MCP 调用必须带 Accept: application/json, text/event-stream 头
#   - user_profile 返回结构: userBasicInfo / interactions / feeds (不再是 basic_info/notes)
#   - get_feed_detail 参数: feed_id (不再是 note_id)
#   - feeds 列表里没有时间字段, 必须逐篇拉详情才能按天过滤
#
# 输出:
#   <out>/<redId>_<nickname>/<redId>_<nickname>_<date>.json
#   <out>/<redId>_<nickname>/images/<note_id>_<idx>.webp
#
# 风控规则:
#   - 任意两次 MCP 调用之间 >=1.5 秒
#   - 命中笔记之间 >=30 秒
#   - 风控关键词命中立即停止: 风控/异常/blocked/forbidden/请稍后再试/verify/登录已过期
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
RISK_KEYWORDS = ["风控", "blocked", "forbidden", "请稍后再试",
                 "verify", "登录已过期", "访问被拒绝"]
# 注意: 移除了 "异常"(太宽泛,正常返回里也含) 和 "login"(check_login_status 的返回 JSON 里就有这个字眼)
# 真正的登录失效用 "登录已过期" 覆盖即可

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


# ============== 带货品类推测 ==============
# 用户账号"小土豆炒股"主业是金融内容,但其他账号也可能涉及。
# 规则: 关键词命中即归类, 多重命中用逗号分隔, 全部不命中填"未识别"。
# 设计原则: 可解释 (每条规则都有明确关键词列表)、保守 (宁漏不错)、如实 (推测不出来就写未识别)。
PRODUCT_CATEGORIES = [
    # 金融/投资类 (用户账号主业,优先识别)
    {
        "category": "金融理财",
        "keywords": [
            "股票", "A股", "港股", "美股", "大盘", "行情", "涨停", "跌停",
            "基金", "ETF", "债券", "可转债", "理财", "定投",
            "开户", "券商", "炒股", "选股", "复盘", "技术分析",
            "期货", "外汇", "黄金", "原油", "大宗商品",
            "财报", "业绩", "估值", "市盈率", "ROE",
            "打新", "新股", "北交所", "科创板", "创业板",
        ],
    },
    # 课程/知识��费类
    {
        "category": "知识付费",
        "keywords": [
            "课程", "训练营", "公开课", "直播课", "网课", "专栏",
            "报名", "学费", "拼课", "团购",
            "知识星球", "公众号", "小红书号", "粉丝群",
            "电子书", "PDF", "学习资料",
            # 考试资料分享类 (小红书常见品类)
            "题库", "教材", "真题", "冲刺班", "网盘群", "资源群", "集训营",
            "执业药师", "执业医师", "事业编", "招警", "考研", "公考",
            "公务员", "教资", "司法考试", "注册会计",
        ],
    },
    # 数码电器类
    {
        "category": "数码电器",
        "keywords": [
            "手机", "iPhone", "华为", "小米", "OPPO", "vivo",
            "耳机", "AirPods", "降噪",
            "电脑", "笔记本", "MacBook", "iPad", "平板",
            "键盘", "鼠标", "显示器",
            "相机", "单反", "微单", "镜头",
            "充电宝", "充电器", "数据线",
            "智能手表", "手表",
        ],
    },
    # 美妆个护类
    {
        "category": "美妆个护",
        "keywords": [
            "口红", "粉底", "散粉", "眼影", "腮红", "高光",
            "面膜", "精华", "面霜", "乳液", "爽肤水", "卸妆",
            "香水", "彩妆", "护肤",
            "洗发水", "护发", "沐浴露", "身体乳",
            "美甲", "美睫",
        ],
    },
    # 服饰鞋包类
    {
        "category": "服饰鞋包",
        "keywords": [
            "连衣裙", "卫衣", "T恤", "衬衫", "西装", "外套",
            "牛仔裤", "阔腿裤", "瑜伽裤", "短裙", "半裙",
            "羽绒服", "毛衣", "针织",
            "运动鞋", "高跟鞋", "靴子", "凉鞋", "拖鞋",
            "包包", "手提包", "双肩包", "钱包",
            "帽子", "围巾", "墨镜", "饰品",
        ],
    },
    # 食品保健类
    {
        "category": "食品保健",
        "keywords": [
            "零食", "坚果", "饼干", "巧克力", "糖果", "蛋糕",
            "茶叶", "咖啡", "奶茶", "果汁",
            "代餐", "蛋白粉", "麦片", "酸奶",
            "保健品", "维生素", "胶原蛋白", "益生菌", "鱼油",
            "减肥", "瘦身", "代糖",
        ],
    },
    # 母婴玩具类
    {
        "category": "母婴玩具",
        "keywords": [
            "婴儿", "宝宝", "孕妇", "孕期",
            "奶粉", "纸尿裤", "辅食",
            "童装", "童鞋", "玩具", "绘本",
            "早教", "启蒙",
        ],
    },
    # 家居生活类
    {
        "category": "家居生活",
        "keywords": [
            "家具", "沙发", "床垫", "床品", "衣柜", "书桌",
            "收纳盒", "收纳袋", "衣架",
            "厨房", "锅具", "刀具", "餐具", "杯子",
            "拖把", "吸尘器", "洗碗机",
            "床上用品", "四件套", "枕头", "被子",
            "摆件", "香薰", "蜡烛",
        ],
    },
    # 运动户外类
    {
        "category": "运动户外",
        "keywords": [
            "跑步", "健身", "瑜伽", "普拉提",
            "运动服", "运动鞋", "运动文胸", "紧身裤",
            "哑铃", "筋膜枪", "健身器材",
            "露营", "帐篷", "睡袋", "登山",
            "自行车", "电动车", "平衡车",
            "游泳", "骑行", "滑雪",
        ],
    },
    # 旅游服务类
    {
        "category": "旅游服务",
        "keywords": [
            "酒店", "民宿", "机票", "高铁", "门票",
            "旅游", "旅行", "攻略", "跟团", "自由行",
            "签证", "出境",
        ],
    },
]


def infer_product_category(title, desc, tags_str=""):
    """
    根据笔记标题/正文/标签推测带货品类。

    返回 (category_str, evidence_str):
        category_str: 命中品类名 (逗号分隔), 或 "未识别"
        evidence_str: 命中的关键词证据 (用于透明度和后续审查)
    """
    if not title:
        title = ""
    if not desc:
        desc = ""
    if not tags_str:
        tags_str = ""
    # 合并所有文本作为分析语料
    text = f"{title} {desc} {tags_str}"

    hits = []
    evidence = []
    for cat in PRODUCT_CATEGORIES:
        cat_name = cat["category"]
        matched_kws = []
        for kw in cat["keywords"]:
            if kw in text:
                matched_kws.append(kw)
        if matched_kws:
            hits.append(cat_name)
            # 只记录前 3 个命中关键词避免太长
            evidence.append(f"{cat_name}({','.join(matched_kws[:3])})")

    if not hits:
        return "未识别", ""

    return ", ".join(hits), "; ".join(evidence)


def refresh_token_via_cdp(red_id, refresh_script=None):
    """
    用 CDP+Edge 自���刷新 redId 的 xsec_token, 返回 profile_url 字符串。
    需要 Edge 已经用 --remote-debugging-port=9222 启动并登录小土豆炒股账号。
    """
    import subprocess
    if refresh_script is None:
        refresh_script = str(Path(__file__).parent / "refresh_token.py")
    if not Path(refresh_script).exists():
        raise RuntimeError(f"refresh_token.py 不存在: {refresh_script}")

    # 临时输出文件
    import tempfile
    tmp_out = Path(tempfile.gettempdir()) / f"xhs_token_{red_id}_{int(time.time())}.json"
    log(f"[refresh] 调用 {refresh_script} 刷新 redId={red_id} 的 token...")

    r = subprocess.run(
        [sys.executable, refresh_script, red_id, "--output", str(tmp_out)],
        capture_output=True, text=True, encoding="utf-8", timeout=180
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"refresh_token.py 退出码 {r.returncode}\n"
            f"stdout: {r.stdout[-800:]}\nstderr: {r.stderr[-500:]}"
        )
    if not tmp_out.exists():
        raise RuntimeError(f"refresh_token.py 完成但未生成输出文件 {tmp_out}")
    token_data = json.loads(tmp_out.read_text(encoding="utf-8"))
    profile_url = token_data.get("profile_url", "")
    if not profile_url or "xsec_token" not in profile_url:
        raise RuntimeError(f"refresh_token.py 输出无效: {token_data}")
    log(f"[refresh] ✅ 成功刷新: nickname={token_data.get('nickname','?')}, "
        f"fans={token_data.get('fans','?')}, token={token_data.get('xsec_token','')[:20]}...")
    # 清理临时文件
    try:
        tmp_out.unlink()
    except Exception:
        pass
    return profile_url


def main():
    ap = argparse.ArgumentParser(description="xhs-collector v2.1 - 采集指定账号指定日期的笔记")
    # 互斥组: --profile-url 或 --red-id 二选一
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile-url",
                       help="小红书主页完整 URL (必须带 xsec_token 参数)。与 --red-id 二选一。")
    group.add_argument("--red-id",
                       help="小红书账号短号 redId (纯数字)。会自动调用 refresh_token.py 通过 CDP+Edge 刷新 token。"
                            "前提: Edge 已用 --remote-debugging-port=9222 启动并登录小土豆炒股。")
    ap.add_argument("--date", required=True,
                    help="目标日期 YYYY-MM-DD (北京时间)")
    ap.add_argument("--out", default="./xhs_output",
                    help="输出根目录 (默认 ./xhs_output)")
    ap.add_argument("--refresh-script",
                    help="refresh_token.py 的路径 (默认同目录下的 refresh_token.py)")
    args = ap.parse_args()

    # 1. 解析 URL —— 两种路径
    if args.profile_url:
        profile_url = args.profile_url
    else:
        # --red-id 模式: 先调用 refresh_token.py 自动刷新
        log(f"[1/6] 通过 CDP+Edge 刷新 redId={args.red_id} 的 token...")
        try:
            profile_url = refresh_token_via_cdp(args.red_id, args.refresh_script)
        except RuntimeError as e:
            log(f"[refresh] 刷新 token 失败: {e}")
            log(f"   前置条件:")
            log(f"   1. Edge 已启动调试端口: msedge --remote-debugging-port=9222 --user-data-dir=<临时目录> --no-first-run https://www.xiaohongshu.com/explore")
            log(f"   2. 在 Edge 里扫码登录小土豆炒股账号 (cookies 会持久化, 之后不用再扫码)")
            log(f"   或者改用 --profile-url 直接传完整主页 URL")
            sys.exit(6)
        log(f"[1/6] token 刷新完成")

    # 解析 user_id 和 xsec_token
    try:
        user_id, xsec_token = parse_profile_url(profile_url)
    except RuntimeError as e:
        log(f"URL 解析失败: {e}")
        log(f"   需要的格式: https://www.xiaohongshu.com/user/profile/<24位hex>?xsec_token=...")
        sys.exit(1)

    log("=" * 60)
    log(f"开始采集 user_id={user_id}, date={args.date}")
    log("=" * 60)

    # 2. MCP 初始化 + 登录检查
    log("[2/6] 初始化 MCP 会话...")
    sid = mcp_init()
    log(f"      session_id = {sid}")

    log("[3/6] 检查登录状态...")
    time.sleep(PACING_API_SEC)
    login = mcp_call(sid, "check_login_status", {}, timeout=180)
    login_text = json.dumps(login, ensure_ascii=False)
    if "已登录" not in login_text and "true" not in login_text.lower():
        log(f"未登录, 请先用 xiaohongshu-login-windows-amd64.exe 扫码")
        sys.exit(1)
    log("      登录正常")

    # 3. 拉账号主页 + feeds 列表
    log(f"[4/6] 拉取账号主页...")
    time.sleep(PACING_API_SEC)
    try:
        account, feeds = fetch_profile(sid, user_id, xsec_token)
    except RuntimeError as e:
        err = str(e)
        log(f"user_profile 失败: {err}")
        if "xsec_token" in err or "token" in err.lower():
            log(f"   提示: xsec_token 可能已失效。")
            if args.red_id:
                log(f"   你用的是 --red-id 自动刷新模式, 但拿到的 token 又失效了,")
                log(f"   可能是 Edge 里的小土豆炒股账号被踢下线, 请重启 Edge 并重新扫码登录。")
            else:
                log(f"   请重新打开网页端复制最新 URL, 或改用 --red-id 自动刷新模式。")
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

    # 4. 逐篇拉详情, 按日期过滤
    log(f"[5/6] 逐篇拉详情过滤日期 {args.date} (共 {len(feeds)} 篇)...")
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
    log("[6/6] 整理结果 + 下载图片...")
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

        # 推测带货品类 (基于标题+正文+话题标签的关键词匹配)
        # 如实记录: 推测不出来就填"未识别"
        note_desc = note.get("desc", "") or ""
        note_title = note.get("title", "") or ""
        # 从正文提取话题标签一起作为推测语料
        tags_for_infer = " ".join(re.findall(r"#([^#\[\]]+)(?:\[话题\])?#?", note_desc))
        product_category, category_evidence = infer_product_category(note_title, note_desc, tags_for_infer)

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
            # 推测字段: 基于内容关键词, 不是官方数据, 仅供运营参考
            "product_category_inferred": product_category,
            "product_category_evidence": category_evidence,
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
    # 推测统计
    identified = sum(1 for n in notes_out if n.get("product_category_inferred", "未识别") != "未识别")
    log(f"   带货品类推测: {identified}/{len(notes_out)} 篇命中")
    if identified > 0:
        from collections import Counter
        cats = Counter()
        for n in notes_out:
            c = n.get("product_category_inferred", "未识别")
            if c != "未识别":
                # 多品类拆开统计
                for sub in c.split(","):
                    cats[sub.strip()] += 1
        for cat, cnt in cats.most_common():
            log(f"     - {cat}: {cnt} 篇")
    log(f"   输出: {out_file}")
    log("=" * 60)


if __name__ == "__main__":
    main()
