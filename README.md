# 疯狂星期四 · astrbot_plugin_crazy_thursday

每周四定时往指定的 QQ 群里丢一条「疯狂星期四」文案。文案源可插拔（vme.im / Telegram 频道经 RSSHub 中转），发送前可选 AI 审核，已发过的文案不会重复。

> 版本：1.0.0 ｜ 适配 AstrBot `>=4.16,<5` ｜ 平台：`aiocqhttp`（QQ 个人号 / NapCat 等）

![logo](logo.png)

## 特性

- **定时发送**：默认每周四 09:00，星期与时间都可在 WebUI 里改；机器人在目标时间后一小时内重启也能补发当天这一条
- **多源可插拔**：默认 `vme` → `rsshub`，前一个失败自动降级；两源都挂时用内置兜底文案，不会空窗
- **最新优先**：vme 源直接读 `vme-im/vme-content` 的 issue 列表（`sort=created&direction=desc`），按发布时间从新到旧取，不再随机
- **不重复**：guid 去重 + 正文指纹去重 + 相邻两条 logo 级排除，池子耗尽才允许重复
- **AI 审核**：发送前用机器人当前使用的 LLM 判断文案，结合"今天"和"计划发送日期"给出结论，涉政、与日期不符的节日文案、违规内容会被跳过
- **管理页面**：状态总览、配置保存、刷新缓存、预览全部文案、手动提前审核
- **单条手动发**：群里发 `/疯狂星期四` 立刻来一条

## 安装

### 方式一：AstrBot 插件市场

在 WebUI「插件」页搜索 `astrbot_plugin_crazy_thursday` 安装。

### 方式二：手动

```bash
cd AstrBot/data/plugins
git clone https://github.com/yoser-s/astrbot_plugin_crazy_thursday.git
```

然后在 WebUI 插件页重载插件（或重启 AstrBot）。

依赖：`httpx`（AstrBot 自带）、`xml.etree.ElementTree`（标准库），无需额外 `pip install`。

## 配置

WebUI → 插件 → 疯狂星期四 → 打开插件页面即可可视化配置；也可以直接改 `data/config/astrbot_plugin_crazy_thursday_config.json`。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `send.groups` | `[]` | 目标群号列表，**必填**，不填不会定时发送 |
| `schedule.weekday` | `4` | 1=周一 … 7=周日，默认 4 即周四 |
| `schedule.time` | `09:00` | 发送时间，24 小时制 |
| `source.provider_order` | `["vme","rsshub"]` | 文案源优先级，逗号或列表 |
| `source.vme_mode` | `latest` | `latest` 走 GitHub issue 列表按时间倒序；`random` 走 `/api/random` |
| `source.vme_repo` | `vme-im/vme-content` | vme 文案仓库（`owner/repo`） |
| `source.vme_api` | `https://vme.im/api/random` | vme 随机接口地址 |
| `source.rsshub_instances` | 3 个公共实例 | RSSHub 实例，按顺序尝试 |
| `source.channel` | `crazythu` | Telegram 频道名（经 RSSHub 读取） |
| `source.timeout` | `12` | HTTP 超时（秒，3–60） |
| `filter.enabled` | `true` | 是否启用发送前 AI 审核 |

> `t.me` 在国内（含容器内）基本不可直连，所以 rsshub 源是通过公共 RSSHub 实例中转的；vme 源则是直连 GitHub API，实测国内容器内 200 / ~1.9s。

## 使用

| 操作 | 位置 |
| --- | --- |
| 立即发一条 | 群里发 `/疯狂星期四` |
| 查看状态 / 下次发送时间 | 插件页面顶部横幅与卡片 |
| 刷新文案缓存 | 插件页面「刷新缓存」（增量合并，不会清空已发送记录） |
| 看所有缓存文案 | 插件页面「预览全部文案」（只读缓存，不联网） |
| 提前跑一遍 AI 审核 | 插件页面「手动审核缓存」（只审未审过的，结果带原因） |

## AI 审核是怎么判的

发送时从最新一条开始逐条送审，第一条通过的就发，单轮最多审 8 条。提示词要求模型结合**今天的日期**与**计划发送日期**判断：

1. 涉政、领导人、政党、体制、历史政治事件、分裂主张，以及影射/隐喻/谐音/戏谑形式的政治话题 → 不通过
2. 内容与某个节日/纪念日强绑定，但发送日期对不上（节日已过或远未到来）→ 不通过；**只是出现节日字样不算问题，应景得体则放行**
3. 色情、暴力、赌博、诈骗、违法引导、辱骂歧视 → 不通过

审核结果按「正文指纹 + 发送日期」缓存于 `data/plugin_data/astrbot_plugin_crazy_thursday/review.json`，同一天不会重复送审；跨周会重新判断。未配置 LLM 或调用失败时自动放行，不会卡住发送。

## 数据文件

运行时数据都在 `data/plugin_data/astrbot_plugin_crazy_thursday/`：

| 文件 | 用途 |
| --- | --- |
| `posts.json` | 文案缓存（保留最近 300 条，含发布时间、首次入库时间） |
| `state.json` | 已发送 guid、上一条 guid、最近正文指纹、上次发送/抓取时间 |
| `review.json` | AI 审核结果缓存 |

删除 `state.json` 会重置去重记录；删除 `posts.json` 会清空文案缓存。

## 目录结构

```
astrbot_plugin_crazy_thursday/
├── main.py                  # 插件主体：抓取、清洗、去重、审核、定时、WebAPI
├── metadata.yaml            # 插件元信息
├── _conf_schema.json         # 配置项定义（WebUI 生成表单用）
├── logo.png                 # 插件图标
├── pages/crazy_thursday/
│   └── index.html           # 管理页面
└── README.md
```

## 常见问题

**Q：为什么默认不用 Telegram 直连？**
`t.me` 在国内网络下不可达（容器内 `Network is unreachable`），因此走 RSSHub 中转；vme 源则完全不依赖 Telegram。

**Q：vme 的 `latest` 和网站首页看到的不一致？**
`latest` 读的是 vme 背后的 GitHub issue 仓库，站点首页只展示已通过其自身审核的条目，所以偶有"仓库里有、首页没有"的文案——这类通常也会被 AI 审核拦下来。

**Q：池子发完了怎么办？**
先退化成只排除上一条，绝不连续发同一条；确实只剩一条可用时才会重复，同时内置兜底文案保证永不为空。

**Q：能改成每天发吗？**
把 `schedule.weekday` 改成想要的星期即可；目前设计是每周一条。

## 更新日志

### 1.0.0

- 定时发送（星期/时间可配置，重启补发）
- vme / RSSHub 双源，自动降级 + 内置兜底
- 按发布时间倒序取最新，guid + 正文指纹双重去重
- 发送前 AI 审核（结合发送日期判断节日文案），支持手动提前审核
- WebUI 管理页面：状态、配置、刷新缓存、预览、手动审核
- 插件图标与 README

## License

MIT
