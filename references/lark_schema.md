# 飞书多维表格 Schema（小红书采集数据）

## Base 信息
- **Wiki 知识���**：小红书虚拟（space_id: `7666846681949801451`）
- **多维表格 Base**：小红书采集数据（base_token: `KbmpbuXoiatEOcsnR5FcYtkJncL`）
- **URL**: https://uxz5jhdn2bg.feishu.cn/wiki/SdJcwNng6iJZD1klZ2ZcXFWXnvb

## 4 张表

### 账号（tbl9YZx9XsG1RoDN）
| 字段名 | 字段ID | 类型 | 说明 |
|---|---|---|---|
| 昵称 | fldBJgCEB4 | text | 账号昵称 |
| redId | fldjogUSFn | text | 小红书号（短ID） |
| user_id | fldqTf2SgP | text | XHS 内部用户ID（24位hex） |
| 简介 | flda5ySkyU | text | 个性签名 |
| IP属地 | fld84GrkzY | text | 主页IP |
| 关注数 | fld2QHOL0m | number | 关注数 |
| 粉丝数 | fldKQnclvG | number | 粉丝数 |
| 获赞与收藏 | fldglhzryg | number | 总互动数 |
| 主页链接 | fldApp9PqH | text (url) | 主页 URL |
| 采集来源 | fldI26enYC | text | 工具/手动 |
| 采集时间 | fldHZs72UV | datetime | 最后采集时间 |
| 头像 | fld9tCQFr2 | attachment | 头像图片 |
| 笔记 | (auto) | link（反向） | 自动从笔记表关联 |

### 笔记（tblsIghwrc2TqemX）
| 字段名 | 字段ID | 类型 |
|---|---|---|
| 所属账号 | fld2qeTDdL | link → 账号 |
| 笔记ID | fld5ePZ1XM | text |
| xsec_token | fld6ZrUB5B | text |
| 标题 | fldxFuocBP | text |
| 正文 | fldPNvFA2J | text |
| IP属地 | fldWEmTOs6 | text |
| 发布时间 | fldWFajvMk | datetime |
| 采集时间 | fldJ8MeRHg | datetime |
| 笔记类型 | fldaujs3Ye | select（图文/视频） |
| 点赞数 | fldFjXAaq0 | number |
| 收藏数 | fldqohUyO2 | number |
| 评论数 | fldL8mq1Nv | number |
| 分享数 | fldvJ3snXS | number |
| 话题标签 | fld0QKCbVv | text |
| 推测带货品类 | fldxr0LEVZ | text |
| 图片附件 | fld5LkQ3yr | attachment |
| 视频URL | fldFNZXoaI | text (url) |
| 视频封面URL | fldz7AFC86 | text (url) |
| 笔记链接 | fld1jzMwik | text (url) |

#### 推测带货品类字段说明
- **数据来源**：基于笔记标题+正文+话题标签做关键词匹配（10 大类目：金融理财/知识付费/数码电器/美妆个护/服饰鞋包/食品保健/母婴玩具/家居生活/运动户外/旅游服务）
- **填充规则**：命中就填（多重命中用逗号分隔）；命中不到就如实填「未识别」
- **不是官方数据**：小红书网页版不展示带货内容（平台机制），这个字段是基于笔记内容做的可解释推测，仅供运营参考
- **JSON 里同时存了 evidence**：collect.py 输出的 JSON 里还有 `product_category_evidence` 字段记录命中的具体关键词（用于审计和后续调参），但飞书表只存最终结果

### 评论（tblJH4LThxzKinzN）
| 字段名 | 字段ID | 类型 |
|---|---|---|
| 所属笔记 | fldYLPbBx0 | link → 笔记 |
| 评论ID | fldATgb4UE | text |
| 内容 | fldhRheyKb | text |
| 用户ID | fldfbIO98X | text |
| 用户昵称 | fldxVJmgS6 | text |
| 用户头像URL | fldWWjMyng | text (url) |
| IP属地 | fldQYX4BSW | text |
| 点赞数 | fldapbsDFW | number |
| 评论时间 | fldk080kNr | datetime |
| 是否作者 | fldo6VhB65 | checkbox |
| 二级回复数 | fldp8GxFvX | number |
| 二级评论JSON | fldyZhzmxV | text |
| 采集时间 | fldEwrfKbT | datetime |

### 商品（tblZQCfpqne3YlAz）— 保留备用，不主动写
完整字段已建好但小红书网页版不展示带货内容（平台机制），按当前决策不主动采集。
- 笔记表里另设有「推测带货品类」字段（fldxr0LEVZ）—— 基于笔记内容关键词匹配做可解释的品类推测，**不是官方带货数据**，仅供运营参考
- 当笔记内容里没有明显品类关键词时，如实填「未识别」

## 关联关系
- 账号 ↔ 笔记（双向，"笔记"反向字段自动维护）
- 笔记 ↔ 评论（双向，"评论"反向字段自动维护）
- 笔记 ↔ 商品（双向，"商品"反向字段自动维护）

## 关键调用约定
- `lark-cli` 命令前缀：`C:\Users\Administrator\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd`
- 身份：`--as user`
- 附件上传：`--file` 必须是**相对路径**（绝对路径会报 "unsafe file path"）
- record-upsert 返回 `data.record.record_id_list[0]`（不是 `record_id`）
- 飞书 API 调用每次间隔 ≥1 秒
