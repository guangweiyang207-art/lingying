<h1 align="center">灵映 · LingYing</h1>

<p align="center">
  <strong>AI 视频创作平台 —— 把一个想法变成一条可以发布的短视频</strong>
  <br>
  小说、剧本或一句创作构想，经由内嵌创作 Agent 编排，自动走完
  资产设定 → 分镜 → 生成 → 合成的完整流水线。
</p>

---

## 这是什么

灵映是一个本地优先的 AI 视频生产工作台：输入小说、成品剧本或商品素材，它会逐步产出角色 / 场景 / 道具资产、分集剧本、分镜图、视频片段与配音，最终合成成片或导出剪映草稿继续精剪。

整个过程由内嵌的创作 Agent（基于 Claude Agent SDK）用自然语言驱动，也可以随时在工作台里人工审核、调整、重做任意一步——关键节点可确认，素材可单独重生成，历史版本可回滚，每一笔模型调用的费用都实时可查。

### 核心特性

- **全链路流水线**：内容分析 → 资产库 → 分集剧本 → 分镜 → 视频 / 音轨 → 成片合成 / 剪映导出，每个阶段可中断、可恢复
- **内嵌创作 Agent**：对话式编排创作流程，支持对话流 WebSocket 实时交互与任务进度 SSE 推送
- **角色一致性**：跨分镜复用参考图，保证同一角色在多镜头下形象稳定
- **多供应商抽象**：文本 / 图像 / 视频 / TTS 四类能力统一接入，当前适配火山方舟、MiniMax 等 11 家供应商
- **费用可追踪**：按次记录每次生成的 token、时长与成本，生成前后均可核对
- **剪映草稿导出**：成片可直接合成，也可导出剪映草稿继续调整字幕、配音与转场
- **模型市场**：内置供应商 / 模型目录与安装管理（独立子包，带格式校验器）

## 架构

```mermaid
flowchart LR
    subgraph frontend ["frontend · React SPA"]
        UI["工作台 / 对话 / 任务面板"]
    end
    subgraph server ["server · FastAPI"]
        API["REST API + WebSocket + SSE"]
        RT["agent_runtime · Claude Agent SDK 封装"]
        W["GenerationWorker 任务队列"]
    end
    subgraph lib ["lib · 核心库"]
        REPO["仓储层 + 项目迁移"]
        PROV["供应商抽象"]
        MKT["market-core / catalog-core"]
    end
    DB[("MySQL / SQLite")]
    UI -->|HTTP / WS| API
    API --> REPO
    RT --> REPO
    W --> PROV
    REPO --> DB
```

| 层 | 技术 |
|---|---|
| 前端 | React + TypeScript + Vite，Vitest 单测 |
| 后端 | Python + FastAPI + SQLAlchemy（async）+ Alembic |
| Agent | Claude Agent SDK，配置源与开发态分离（`agent_runtime_profile/`） |
| 数据库 | 默认 SQLite，支持 MySQL 5.7+（方言适配集中在 `lib/db/mysql_compat.py`） |
| 工具链 | uv（Python）/ pnpm（前端），`scripts/gate.py` 按域跑质量闸门 |

## 快速开始

环境要求：Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)、Node 20+ 与 pnpm。

```bash
git clone https://github.com/guangweiyang207-art/lingying.git
cd lingying

# 1. 配置
cp .env.example .env          # 按注释填写；AUTH_PASSWORD 留空则首次启动自动生成
uv sync                       # 安装后端依赖

# 2. 启动后端（端口 1241）
uv run uvicorn server.app:app --port 1241

# 3. 另开终端，启动前端
cd frontend
pnpm install
pnpm dev                      # http://localhost:5173
```

打开 <http://localhost:5173>，用户名 `admin`、密码取自 `.env` 的 `AUTH_PASSWORD`（首次启动自动生成并回写）。登录后先在 **设置** 页配置模型供应商的 API Key，再创建项目开始创作。

### 数据库与数据目录

默认使用 SQLite，开箱即用；需要 MySQL 时在 `.env` 中改为：

```ini
DATABASE_URL=mysql+aiomysql://user:pass@host:3306/lingying?charset=utf8mb4
```

表结构由 Alembic 管理（`uv run alembic upgrade head`）；从 SQLite 换 MySQL 可用 `scripts/migrate_sqlite_to_mysql.py` 一次性搬数。生成素材、项目文件的存放位置由 `ARCREEL_DATA_DIR` 指定。

## 说明

- 请在各模型供应商处自行开通账号并配置 API Key，视频 / 图像生成按量计费，建议先用少量分镜试产再放量。
- 剪映草稿导出面向中国大陆版剪映，CapCut 兼容性未验证。

## 许可证

[AGPL-3.0](LICENSE)
