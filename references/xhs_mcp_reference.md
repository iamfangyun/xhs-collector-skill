# xiaohongshu-mcp 工具参考

## 工具位置
- **二进制**：`C:\Users\Administrator\WorkBuddy\2026-07-24-22-52-09\xiaohongshu-mcp-windows-amd64.exe`
- **登录工具**：`xiaohongshu-login-windows-amd64.exe`（同目录）
- **MCP URL**：`http://localhost:18060/mcp`
- **cookies**：`cookies.json`（启动 exe 时所在目录）

## 启动流程
```bash
# 1. 启动主服务（headless 模式）
cd <工作目录>
./xiaohongshu-mcp-windows-amd64.exe -port ":18060"

# 2. 单独扫码登录（首次或 cookie 过期）
./xiaohongshu-login-windows-amd64.exe
# 扫码后 cookies.json 生成在工作目录
```

## MCP Streamable HTTP 调用协议
1. POST `initialize`，从响应头抓 `Mcp-Session-Id`（注意大小写，全大写带连字符）
2. POST `notifications/initialized`（带 session-id 头）
3. POST `tools/call`，必须带 `Mcp-Session-Id` 请求头

完整 curl 示例：
```bash
# 1. initialize
SID=$(curl -s -D - -o /dev/null -X POST http://localhost:18060/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"x","version":"1.0.0"}}}' \
  | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r\n')

# 2. initialized notification
curl -s -X POST http://localhost:18060/mcp \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

# 3. tools/call
curl -s -X POST http://localhost:18060/mcp \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"check_login_status","arguments":{}}}'
```

## 13 个工具速查
| 工具 | 用途 | 关键参数 |
|---|---|---|
| `check_login_status` | 检查登录 | 无 |
| `get_login_qrcode` | 拿二维码（headless 下不可用，用 login exe） | 无 |
| `delete_cookies` | 删除 cookies | 无 |
| `user_profile` | **核心**：账号信息 + 笔记列表 | `user_id` + `xsec_token` |
| `get_feed_detail` | **核心**：笔记详情 + 评论 + 图片URL | `feed_id` + `xsec_token` + `load_all_comments` + `limit` + `scroll_speed` |
| `list_feeds` | 首页 feed | 无 |
| `search_feeds` | 搜索 | `keyword` + `filters` |
| `like_feed` | 点赞/取消 | `feed_id` + `xsec_token` + `unlike` |
| `favorite_feed` | 收藏/取消 | `feed_id` + `xsec_token` + `unfavorite` |
| `post_comment_to_feed` | 发评论 | `feed_id` + `xsec_token` + `content` |
| `reply_comment_in_feed` | 回复评论 | `feed_id` + `xsec_token` + `comment_id` + `user_id` + `content` |
| `publish_content` | 发图文笔记 | `title` + `content` + `images` + `tags` |
| `publish_with_video` | 发视频笔记 | `title` + `content` + `video` |

## user_profile 响应结构
```json
{
  "data": {
    "user": {
      "userId": "67e61076000000000601d2cf",
      "nickname": "满分💯课代表",
      "redId": "95466594071",
      "desc": "...",
      "ipLocation": "广东",
      "avatar": "https://sns-avatar-qc.xhscdn.com/..."
    },
    "interactions": {
      "follows": 8,
      "fans": 43,
      "interaction": 330
    },
    "notes": [
      {
        "noteId": "6a60d814000000001b01f2d4",
        "xsecToken": "ABueIqy7SJmoDkSaz11GlVEgQZwrtuiS2uLu488tHFP80=",
        "title": "...",
        "type": "normal",
        "time": 1784731668000,
        ...
      }
    ]
  }
}
```

## get_feed_detail 响应结构
```json
{
  "data": {
    "note": {
      "noteId": "...",
      "xsecToken": "...",
      "title": "...",
      "desc": "...",
      "type": "normal|video",
      "time": 1784731668000,  // 毫秒时间戳
      "ipLocation": "广东",
      "user": { "userId": "...", "nickname": "...", "avatar": "..." },
      "interactInfo": {
        "likedCount": "1", "collectedCount": "2",
        "commentCount": "3", "sharedCount": "0"
      },
      "imageList": [
        {
          "width": 1080, "height": 1350,
          "urlDefault": "http://sns-webpic-qc.xhscdn.com/.../webp_3"
        }
      ]
    },
    "comments": {
      "list": [
        {
          "id": "6a5e2ced00000000050175bb",
          "content": "...",
          "likeCount": 0,
          "createTime": 1784556782000,  // 毫秒
          "ipLocation": "广东",
          "userInfo": { "userId": "...", "nickname": "...", "avatar": "" },
          "subCommentCount": 0,
          "subComments": [],
          "showTags": ["is_author"]
        }
      ],
      "hasMore": false,
      "cursor": ""
    }
  }
}
```

## 字段命名陷阱
- 评论里的用户字段是 `userInfo`（不是 `user`）
- 图片 URL 字段是 `urlDefault`（不是 `url_default`）
- 时间戳都是毫秒
- `interactInfo.commentCount` 可能是空字符串 `""`（接口不暴露），也可能是数字字符串如 `"1"`

## 风控信号
出现以下关键词立即停止：
- "风控"、"异常"、"blocked"、"forbidden"
- "请稍后再试"、"verify"
- HTTP 状态非 200
- cookies.json 被服务端清空

## 商品采集限制
`get_feed_detail` 返回字段固定，**没有 goods/productInfo 等商品字段**。小红书网页版也不展示带货内容。商品表 schema 在飞书已建好但保持空表，不主动采集。
