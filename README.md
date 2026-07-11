# BlogReader · 个人博客阅读器

把散落在各处的个人博客聚合到一起：粘贴一个博客网址，自动发现它的 RSS/Atom feed，定时抓取更新，在一个统一的界面里阅读、标记已读、收藏归档，也能翻阅历史文章。

面向个人自用，本地一键启动，零前端构建（不需要 Node）。

## 特性

- 自动发现 feed：粘贴博客主页网址即可，会从 HTML 的 `<link rel="alternate">` 和常见路径（/index.xml、/feed、/rss.xml 等）里找到 feed
- 定时抓取：后台每 30 分钟自动刷新所有订阅（可配），也可手动刷新
- 统一阅读：三栏界面（订阅源 / 文章列表 / 正文），支持未读圆点、未读数徽标
- 已读 & 星标：打开文章自动标已读，可手动切换；星标收藏便于归档
- 过滤：全部 / 未读 / 星标
- 去重：按文章 guid 去重，重复抓取不会产生重复条目
- 全文抓取：feed 只给摘要时，自动回源抓取正文全文（trafilatura 提取正文，保留图注、表格、行内代码）
- 数学公式：正文里的 LaTeX 公式用 MathJax 渲染
- 分段对照翻译：可接入 OpenAI / Anthropic 规范的接口，滚动到哪段就翻译哪段，中外分段对照展示（API key 存本地，不外传）
- 本地存储：SQLite 单文件数据库，零配置

## 技术栈

- 后端：FastAPI + uvicorn
- 抓取：httpx + feedparser + beautifulsoup4（feed 自动发现）
- 存储：SQLite + SQLModel
- 定时：APScheduler
- 前端：Vue 3（CDN 引入，免构建）+ 原生 fetch，由 FastAPI 托管静态文件

## 目录结构

```
blogreader/
├── backend/
│   ├── main.py          # FastAPI 应用 + API 路由 + 前端托管
│   ├── models.py        # Feed / Article 数据模型
│   ├── db.py            # SQLite 引擎与初始化
│   ├── feed_service.py  # feed 自动发现 + 抓取解析入库
│   └── scheduler.py     # APScheduler 后台定时刷新
├── frontend/
│   └── index.html       # Vue3 单页应用（三栏阅读界面）
├── data/
│   └── blogreader.db    # SQLite 数据库（首次运行自动生成）
├── requirements.txt
└── run.py               # 启动入口
```

## 快速开始

1. 安装依赖（建议用虚拟环境）：

   ```bash
   pip install -r requirements.txt
   ```

2. 启动：

   ```bash
   python run.py
   ```

3. 浏览器打开 http://127.0.0.1:8000

4. 在左栏输入框粘贴一个博客网址（例如 `https://lilianweng.github.io/`），回车或点 `+`，稍等片刻就能看到抓取到的文章。

### 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BLOGREADER_HOST` | `127.0.0.1` | 监听地址 |
| `BLOGREADER_PORT` | `8000` | 监听端口 |
| `BLOGREADER_INTERVAL_MIN` | `30` | 后台自动刷新间隔（分钟） |

### 分段对照翻译（可选）

翻译功能需要你自备一个 LLM 接口，支持 OpenAI 或 Anthropic 规范：

1. 打开阅读器右上角的「翻译设置」，填写 `api_type`（openai / anthropic）、`base_url`、`api_key`、`model`。
2. 配置保存在本地 SQLite 的设置表里，仅本机使用；后端接口只返回「是否已配置」，不会回传 api_key。
3. 在文章正文点「翻译」，滚动到哪段就按需翻译哪段，中外分段对照展示。

> API key 是你自己的凭证，明文存本地库（个人自用场景）。若要部署到多人环境，请自行改造为加密存储并加访问控制。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/feeds` | 添加订阅（body: `{"url": "..."}`，自动发现 feed） |
| GET | `/api/feeds` | 订阅列表（含未读数、总数、抓取状态） |
| DELETE | `/api/feeds/{id}` | 删除订阅及其文章 |
| POST | `/api/feeds/{id}/refresh` | 刷新单个订阅 |
| GET | `/api/articles` | 文章列表（`feed_id`、`filter=all\|unread\|starred`、`limit`、`offset`） |
| GET | `/api/articles/{id}` | 文章详情（含正文） |
| POST | `/api/articles/{id}/read` | 标记已读/未读（body: `{"is_read": true}`） |
| POST | `/api/articles/{id}/star` | 星标/取消（body: `{"is_starred": true}`） |
| POST | `/api/refresh` | 手动刷新所有订阅 |

## 说明与边界

- 仅供个人使用。抓取请遵守目标站点的 robots 与使用条款，控制刷新频率，避免对小型个人站点造成压力。
- 正文以 feed 提供的内容为准。有些博客的 feed 只给摘要，正文会相应较短，可点「原文」跳转阅读。
- 单机单用户设计，未做鉴权。如需部署到公网服务器，请自行在前面加反向代理与访问控制。

## 后续可做

- 全文搜索、标签分类
- feed 只给摘要时抓取原文全文
- 未读/星标的导出与备份
- OPML 导入导出（迁移已有订阅）

## 关于本项目

本项目由 [GenericAgent](https://github.com/lsdefine/GenericAgent)（GA）**完全自主开发**——从需求分析、架构设计、编码实现到调试验证，全部由 GA 独立完成。

## 许可证

[MIT](LICENSE)
