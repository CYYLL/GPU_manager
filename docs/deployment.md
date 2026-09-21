# GPU Resource Manager v2 — 部署与配置文档

> 版本 2.0 | 2026-09-07
>
> 代码库：`/amax/gpu_manager_v2`（v1 的演进分支，与运行中的 v1 `/amax/gpu_manager` **并存独立**，各用各的 SQLite 数据库）

---

## 目录 / Table of Contents

1. [项目概述 / Overview](#1-项目概述--overview)
2. [v2 变更速查 / What's new in v2](#2-v2-变更速查--whats-new-in-v2)
3. [环境配置 / Environment Config (.env)](#3-环境配置--environment-config-env)
4. [项目结构 / Project Structure](#4-项目结构--project-structure)
5. [部署 / Deployment](#5-部署--deployment)
6. [API 接口 / API Endpoints](#6-api-接口--api-endpoints)
7. [常见问题 / FAQ](#7-常见问题--faq)

---

## 1. 项目概述 / Overview

GPU Manager 是一个基于 Web 的 GPU 资源管理平台，支持多用户 GPU 容器调度、配额管理和实时监控。v2 在 v1 基础上新增三条主线能力：

- **双模式（Dual-mode）**：每个 `User` 有一个 `mode`（`llm` / `traditional`，见 `MODE_DEFAULT`）。`llm` 用户可在前端 Agent 页用自然语言指挥 agent 完成 GPU/容器操作；`traditional` 走传统手动页面。`mode` 由服务端 `User.mode` 权威存储，`GET/PUT /api/mode` 读写。
- **自主监控（Monitor）**：后端定时线程周期性采样 GPU/容器/磁盘状态、写 `ContainerEvent` / `DiskSnapshot` 表、按保留期清理旧事件与聊天记录。
- **自动清理引擎（Cleanup）**：磁盘水位触发时，按 LRU 清理「仅删容器、保留工作区」的最久未用容器：容器置 `removed` 态并保留配置快照，可一键 rebuild；支持清理保护标记、dry-run 预演、容量/扩容 admin 告警。

Key features (v1 base + v2):

- Real-time GPU monitoring via NVML
- Multi-tenant GPU allocation with quota enforcement
- Docker container lifecycle with GPU pass-through, stop-retry
- LLM agent (chat + streaming SSE + 12 mutating tools) for container ops
- Automatic disk cleanup (LRU, protection, rebuild, dry-run)
- Role-based access control (user / admin); llm-mode gating on agent & admin surfaces
- Preset GPU Docker image management; SSH access into containers

### 1.1 推荐镜像 / Recommended Images
Ubuntu 20.04 / 22.04 / 24.04 · Debian 11 / 12 · CUDA 官方镜像 · PyTorch 官方镜像 · TensorFlow 官方镜像 · 基于 Ubuntu/Debian 构建的 AI 训练镜像

### 1.2 数据库 / Database

- 表由 `Base.metadata.create_all` 在启动时**自动创建**（无迁移工具，属设计决策）。
- 默认 SQLite：`DATABASE_URL` 未设置时回退 `sqlite:///./gpu_resource_manager.db`，**相对路径按 systemd `WorkingDirectory`（= `backend/`）解析** → 数据库文件位于 `backend/gpu_resource_manager.db`。
- ⚠ v2 用的是**自己全新的空库**，不携带 v1 的用户/容器/配额数据（schema 比 v1 多出 `User.mode`、`ContainerInstance.cleanup_protected` 等列，直接拷贝 v1 库文件**不受支持**，见 §7.4）。
- v2 数据表：`users, gpu_images, container_instances, gpu_allocations, container_events, disk_snapshots, cleanup_logs, chat_messages, admin_alerts`。

---

## 2. v2 变更速查 / What's new in v2

| 变更 | 说明 | 相关章节 |
|------|------|----------|
| **LLM agent** | `/chat` 页自然语言操作容器/GPU/镜像/存储；SSE 流式回复 + 工具调用轨迹 | §6 Agent |
| **双模式 mode** | `User.mode` = `llm`/`traditional`，后端权威 + 前端 ModeContext 缓存 | §3、§6 Mode |
| **清理引擎** | 磁盘超阈值时 LRU 清理；**仅删容器保留工作区** | §3 Cleanup、§6 Monitor/Admin |
| **removed 一键 rebuild** | 被清理容器状态=removed，可 POST `/rebuild` 按快照重建 | §6 容器 |
| **清理保护** | `cleanup_protected` 容器永不进入清理候选 | §6 容器 |
| **监控/告警** | `monitor` 线程采样 + `ContainerEvent`/`DiskSnapshot`；容量/扩容 `AdminAlert` | §3、§6 |
| **llm-mode 门控** | `traditional` 模式调 agent/监控/候选接口 → 403（后端 `require_llm_mode`） | §6 |
| **SSE 反代缓冲关闭** | nginx 对 `/api/` 关 `proxy_buffering`，流式即时送达 | §5.1、§5.2 |

---

## 3. 环境配置 / Environment Config (.env)

配置文件位于**项目根目录** `./.env`（`/amax/gpu_manager_v2/.env`）。后端 `app/main.py` 在导入任何 router **之前**用 `load_dotenv(PROJECT_ROOT/.env)` 加载（模块级构造 `LLMClient`/`DockerRunner` 在 import 时读 env）。

> ⚠ systemd 单元里 `Environment=` 的值会**覆盖** `.env`（dotenv 默认不覆盖已有进程环境变量）——因此 v2 的单元文件**不写密钥**，所有配置以 `.env` 为准。改 `.env` 后重启后端生效。

### 3.1 完整参考 / Full reference（按类别）

**① 安全 / Security**

| 变量 | 默认 | 说明 |
|------|------|------|
| `SECRET_KEY` | `your-secret-key-change-in-production` | JWT 签名密钥。**生产必须改**：`openssl rand -hex 32`。泄露即可伪造任意管理员 token |
| `ALGORITHM` | `HS256` | JWT 签名算法 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | 登录 token 有效期（分钟） |
| `CORS_ORIGINS` | `*` | 允许跨域来源，逗号分隔；生产设具体域名 |

**② 数据库 / Database**

| 变量 | 默认 | 说明 |
|------|------|------|
| `DATABASE_URL` | `sqlite:///./gpu_resource_manager.db` | 连接串。SQLite 相对路径按 cwd（backend/）解析；PostgreSQL 示例见下 |

```
DATABASE_URL=postgresql://user:password@host:5432/gpu_manager
```

**③ 容器挂载 / 资源分配**

| 变量 | 默认 | 说明 |
|------|------|------|
| `CONTAINER_MOUNT_ROOT` | `/amax` | 容器工作目录父路径；`gpu-{username}/` 挂到容器 `/workspace`，**必须绝对路径** |
| `ALLOCATION_TIMEOUT` | `60` | GPU 分配锁超时（秒），并发申请串行排队 |
| `PORT_RANGE_START` / `PORT_RANGE_END` | `22000` / `22999` | 容器 SSH 映射宿主端口范围 |
| `STALE_MEMORY_THRESHOLD` | `5` | 显存利用率低于此百分比视为空闲（检测悬空 GPU） |

**④ Docker 停止重试**

| 变量 | 默认 | 说明 |
|------|------|------|
| `DOCKER_STOP_ATTEMPTS` | `3` | stop/remove 总尝试次数（含首次） |
| `DOCKER_STOP_TIMEOUT` | `10` | 单次 stop 超时（秒） |
| `DOCKER_STOP_RETRY_DELAY` | `2` | 重试间隔基数（秒），第 n 次间隔 = 基数 × n |

**⑤ 双模式 / Agent（v2）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `MODE_DEFAULT` | `llm` | 新注册用户的默认模式：`llm` 或 `traditional`（可被前端模式切换覆盖，写入该用户 `User.mode`） |
| `LLM_API_KEY` | （空） | Agent 的 LLM API Key。优先级高于 `ANTHROPIC_AUTH_TOKEN` |
| `LLM_BASE_URL` | （空） | 自定义网关/代理地址（OpenAI 兼容转发等）。优先级高于 `ANTHROPIC_BASE_URL` |
| `LLM_MODEL` | `claude-sonnet-4-6`* | 模型名（配置后优先生效） |
| `ANTHROPIC_BASE_URL` | （空） | Anthropic API 地址（默认官方） |
| `ANTHROPIC_AUTH_TOKEN` | （空） | Anthropic API Key |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | （空） | 未设 `LLM_MODEL` 时的兜底模型名 |
| `LLM_STREAMING` | `auto` | `auto`/`true` 走原生流式；`false` 关流式（走一次性 complete） |

\* 代码内置兜底；三者均未设置时使用内置默认名。

**⑥ 监控与保留（v2）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `MONITOR_INTERVAL` | `3600` | 监控/清理线程采样间隔（秒）。生产按需调小（如 `300`）以更快发现外部停止/水位 |
| `EVENT_RETENTION_DAYS` | `30` | `ContainerEvent` 保留天数 |
| `SNAPSHOT_RETENTION_DAYS` | `7` | `DiskSnapshot` 保留天数 |
| `CHAT_HISTORY_LIMIT` | `200` | 每用户保留的最近 chat 消息条数 |

**⑦ 清理引擎（v2）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `CLEANUP_ENABLED` | `true` | 总开关（`0`/`false`/`no` 关） |
| `CLEANUP_DRY_RUN` | `false` | 预演模式：只记录决策、不实际删容器不回收空间（见 §7.5 消耗说明） |
| `DISK_THRESHOLD` | `85` | 磁盘水位（%）触发清理 |
| `DISK_CRITICAL` | `95` | 临界水位（%）；达到时更激进 |
| `DISK_TARGET` | `80` | 常规清理目标水位（%） |
| `DISK_CRITICAL_TARGET` | `90` | 临界时目标水位（%） |
| `CLEANUP_COOLDOWN_MINUTES` | `5` | 两轮清理最小间隔 |
| `CLEANUP_MAX_PER_DAY` | `10` | 每 24h（UTC 0 点起）最多删容器数 |
| `CLEANUP_MAX_PER_ROUND` | `3` | 每轮最多删容器数 |
| `CONTAINER_RECLAIM_TRIGGER_GB` | `20` | 容器（SizeRw/运行 SizeRootFs）+ 可回收镜像 ≥ 此 GB 才触发 docker 侧清理 |
| `BUILD_CACHE_TRIGGER_GB` | `100` | BuildKit 缓存 ≥ 此 GB 时独立执行 builder prune |
| `MIN_EFFECTIVE_FREE_GB` | `5` | 最小有效净释放；低于则升级容量告警 |
| `WORKSPACE_DOMINANT_PCT` | `60` | `/amax` 下 gpu-* 工作区占全盘比例超过此值 → 建议扩容告警（删除无效） |
| `GRACE_DAYS` | `2` | 未使用宽限期：停止的容器空闲超过此天数才入候选 |
| `CLEANUP_LOCK_FILE` | `/tmp/gpu_manager_v2_cleanup.lock` | 跨进程清理互斥锁文件（`fcntl.flock`），避免多实例并发清理 |

**⑧ 闲置 GPU 自动停止（v2）**

| 变量 | 默认 | 说明 |
|------|------|------|
| `IDLE_GPU_ENABLED` | `true` | 总开关（`0`/`false`/`no` 关） |
| `IDLE_GPU_INTERVAL_SECONDS` | `1800` | 扫描间隔（秒），约每 30 分钟一轮 |
| `IDLE_GPU_HOURS` | `8` | 持续空闲（该容器所有卡利用率均 =0 且显存 ≤ 阈值）满此小时数 → 自动停容器 |
| `IDLE_GPU_MEM_IDLE_PCT` | `5` | 显存占用低于该百分比才视为空闲（与 GPU 看板 free 口径一致） |
| `IDLE_GPU_DRY_RUN` | `false` | 预演模式：判定到期的容器只计数 `planned`，不停容器、不留事件/通知 |

> 行为要点：只作用于 **running** 容器；多卡容器须 **全部** 卡同周期空闲才算一个 idle tick，任一卡忙碌即清空累计，采样缺失/不可读一律不判定（绝不误停）。满窗口后动作前会做一次 **新鲜 NVML 复检**，已恢复使用则不停止。停止 = docker stop（保留容器）→ 释放 GPU 分配 → 置 stopped → 记 `ContainerEvent(事件=stop, source=agent)` → 向所属用户 Agent 会话插入一条系统通知。`cleanup_protected` **不豁免**。持久标记存于新增表 `container_idle`（服务重启自动 `create_all` 建表，无需迁移）。

在 v2 项目根目录放 `.env`（**不是** backend/ 下）。`DATABASE_URL` 保持注释即用默认 SQLite。

```bash
# ── Security ──
SECRET_KEY=openssl rand -hex 32 产生的值
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# ── CORS ──
CORS_ORIGINS=http://localhost:3000,http://localhost,http://127.0.0.1

# ── Container mount root / GPU allocation / ports ──
CONTAINER_MOUNT_ROOT=/amax
ALLOCATION_TIMEOUT=60
PORT_RANGE_START=22000
PORT_RANGE_END=22999
STALE_MEMORY_THRESHOLD=5

# ── Docker stop retry ──
DOCKER_STOP_ATTEMPTS=3
DOCKER_STOP_TIMEOUT=10
DOCKER_STOP_RETRY_DELAY=2

# ── Database (default: SQLite under backend/) ──
# DATABASE_URL=sqlite:///./gpu_resource_manager.db

# ── Dual-mode ──
MODE_DEFAULT=llm            # llm | traditional

# ── LLM / Agent ──
LLM_API_KEY=sk-...
# LLM_BASE_URL=https://api.anthropic.com
# LLM_MODEL=claude-sonnet-4-6
# LLM_STREAMING=auto
# ANTHROPIC_BASE_URL=
# ANTHROPIC_AUTH_TOKEN=
# ANTHROPIC_DEFAULT_SONNET_MODEL=

# ── Monitor & retention ──
MONITOR_INTERVAL=300
EVENT_RETENTION_DAYS=30
SNAPSHOT_RETENTION_DAYS=7
CHAT_HISTORY_LIMIT=200

# ── Cleanup engine ──
CLEANUP_ENABLED=true
CLEANUP_DRY_RUN=false
DISK_THRESHOLD=85
DISK_CRITICAL=95
DISK_TARGET=80
DISK_CRITICAL_TARGET=90
CLEANUP_COOLDOWN_MINUTES=5
CLEANUP_MAX_PER_DAY=10
CLEANUP_MAX_PER_ROUND=3
CONTAINER_RECLAIM_TRIGGER_GB=20
BUILD_CACHE_TRIGGER_GB=100
MIN_EFFECTIVE_FREE_GB=5
WORKSPACE_DOMINANT_PCT=60
GRACE_DAYS=2
# CLEANUP_LOCK_FILE=/tmp/gpu_manager_v2_cleanup.lock

# ── Idle-GPU auto-stop ──
IDLE_GPU_ENABLED=true
# IDLE_GPU_INTERVAL_SECONDS=1800
IDLE_GPU_HOURS=8
IDLE_GPU_MEM_IDLE_PCT=5
# IDLE_GPU_DRY_RUN=false
```

---

## 4. 项目结构 / Project Structure

```
/amax/gpu_manager_v2/
├── .env                          # 环境配置（项目根目录，main.py 从这里 load_dotenv）
├── backend/
│   ├── app/
│   │   ├── main.py               # 入口：先 load_dotenv(PROJECT_ROOT/.env) 再 import routers
│   │   ├── database.py           # engine + get_db（DATABASE_URL 默认 sqlite:///./…，按 cwd 解析）
│   │   ├── models.py             # 10 张表（User/GpuImage/ContainerInstance/GpuAllocation/
│   │   │                         #   ContainerEvent/ContainerIdleState/DiskSnapshot/CleanupLog/
│   │   │                         #   ChatMessage/AdminAlert）
│   │   ├── schemas.py            # Pydantic 模型（UserOut.mode、ContainerResponse.cleanup_protected …）
│   │   ├── auth.py               # JWT + get_current_user / get_current_admin
│   │   ├── routers/
│   │   │   ├── users.py          # 注册/登录/me/密码 + admin 用户/配额
│   │   │   ├── gpus.py           # GPU 状态 + admin 分配记录
│   │   │   ├── containers.py     # 容器生命周期 + rebuild + protection + 镜像
│   │   │   ├── mode.py           # GET/PUT /api/mode + require_llm_mode 依赖（llm 门控）
│   │   │   ├── agent.py          # chat / chat/stream(SSE) / session
│   │   │   └── monitor.py        # monitor 容器/磁盘/候选 + admin 清理日志/告警
│   │   ├── services/
│   │   │   ├── gpu_monitor.py    # NVML 只读状态
│   │   │   ├── gpu_allocator.py  # GPU 分配/配额（线程锁）
│   │   │   └── docker_runner.py  # Docker 操作（df/prune/build-prune 透传 + stop 重试）
│   │   ├── agent/                # v2：agent 子系统
│   │   │   ├── llm_client.py     # LLMClient（LLM_* 覆盖 ANTHROPIC_*；complete/stream）
│   │   │   ├── system_prompt.py  # 系统提示 + 变更能力护栏
│   │   │   ├── tools.py          # 11 个工具（查询：容器/GPU/磁盘/镜像；变更：create/start/stop/
│   │   │   │                     #   remove/rebuild/set_protection）
│   │   │   ├── executor.py       # 工具执行器
│   │   │   ├── monitor.py        # 采样线程：snapshot/retention + 每轮调 cleanup
│   │   │   ├── cleanup.py        # 清理引擎：决策 + 控制循环（锁/限额/冷却/告警）
│   │   │   └── idle_gpu.py       # 闲置 GPU 自动停止线程：每轮扫 running 容器，持续空闲满窗口即停（可 env 关闭/dry-run）
│   │   └── crud/
│   │       ├── users.py
│   │       └── containers.py     # 容器/分配/镜像/事件/清理日志 CRUD + mark_container_removed
│   ├── requirements.txt
│   ├── gpu_resource_manager.db   # v2 自己的 SQLite（自动建表）
│   └── tests/                    # pytest（Phase 回归基线 93 passed）
├── frontend/                     # React 18 SPA
│   ├── src/
│   │   ├── context/ModeContext.js# mode 服务端同步 + localStorage 缓存
│   │   ├── components/ModeToggle.js  # LLM/传统 切换
│   │   ├── pages/AgentChat.js    # agent 聊天（SSE 流式 + 工具轨迹）
│   │   ├── pages/AdminCleanupLog.js   # /admin/cleanup 监控+清理日志+告警
│   │   ├── services/agentStream.js    # fetch POST SSE 解析器
│   │   └── …                    # 其余同 v1（Home/Dashboard/ContainerManagement/…）
│   └── build/                    # npm run build 产物（nginx 直读）
├── deploy/                       # v2 部署产物（见 §5）
│   ├── gpu-manager-backend.service
│   ├── gpu-manager-frontend.service
│   ├── gpu-manager.nginx.conf
│   ├── install-services.sh
│   └── gpu-manager-v2-cutover.sh # v1→v2 停机窗口切换（幂等 + 备份）
└── docs/deployment.md            # 本文档
```

---

## 5. 部署 / Deployment

### 5.0 线上现状（本机）

`/amax/gpu_manager`（v1）的 `gpu-manager-backend.service` **正在 :8000 运行**，nginx 服务其前端。v2 与其完全独立：不同代码目录、不同 `.env`、不同 SQLite、**未占用的进程**。下列步骤在生产切换前均不会触碰 v1。

### 5.1 全新部署 v2 / First-time on a fresh host

以下命令在 `/amax/gpu_manager_v2/` 下执行。

```bash
# 1. 后端依赖（本机已装；新机用项目 Python 环境）
cd backend
pip install -r requirements.txt

# 2. 前端依赖 + 构建（Node 18 via nvm；本机默认 Node 10 过旧）
cd ../frontend
source ~/.nvm/nvm.sh && nvm use 18
npm install
CI=1 npm run build          # 产物 → frontend/build/

# 3. .env（项目根目录）——至少改 SECRET_KEY，LLM 场景填 LLM_API_KEY 等（§3.2）
cd ..
#   （编辑 .env）

# 4. 冒烟启动（占 8001，不冲突线上 8000）
cd backend
/opt/anaconda3/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
#   另开终端：curl http://127.0.0.1:8001/docs   → 200；Ctrl+C 停掉

# 5. 用 deploy 安装 systemd + nginx
sudo bash deploy/install-services.sh
```

`install-services.sh` 语义（本仓库 deploy 内）：
- **检测到 v1 在跑（:8000 被非 v2 占用）→ 拒绝并退出**，提示走 cutover 脚本；不会静默覆盖线上配置造成「半切换」状态。
- 无冲突（新机 / 已切换为 v2）→ 安装并 enable+start `gpu-manager-backend`（:8000）与 `gpu-manager-frontend`（:3001，可选，nginx 直读 build），拷贝 nginx 站点、`nginx -t && reload`。
- nginx 配置含对 `/api/` 的 `proxy_buffering off`（否则 agent 流式 SSE 会整段缓冲）。

### 5.2 v1 → v2 切换（停机窗口）/ v1 → v2 cutover

> 这是**运维动作**，本仓库只提供受控脚本与 runbook，**不自动执行**。切换会中断线上服务几十秒。

前置：
- [ ] 已在 `/amax/gpu_manager_v2/.env` 配置 SECRET_KEY 及 LLM/监控/清理所需变量
- [ ] 已 `npm run build`（`frontend/build` 存在）
- [ ] 已备份 v1（`/amax/gpu_manager` 原样保留即可；cutover 还会把当前 systemd/nginx 配置备份到 `deploy/backup/<时间戳>/`）
- [ ] ⚠ **数据不迁移**：v2 启动是新空库，无任何用户。切换后需通过 UI/API 重新注册用户、admin 重新分配配额。直接拷贝 v1 `gpu_resource_manager.db` 不受支持（schema 含 v2 新列），需要时按 §7.4 自行迁移。

窗口内执行：

```bash
cd /amax/gpu_manager_v2
sudo bash deploy/gpu-manager-v2-cutover.sh          # 交互确认
# 或跳过确认：
sudo bash deploy/install-services.sh --cutover
```

脚本步骤（幂等）：备份现有单元/nginx → `disable --now` 停止 v1 的 `gpu-manager-backend`/`gpu-manager-frontend` → 安装 v2 单元并 `enable --now`（backend 接管 :8000）→ nginx 站点切到 v2 → `nginx -t && reload` → 打印服务状态 + HTTP 探测（`/docs`、`/`）。

验证：

```bash
sudo systemctl is-active gpu-manager-backend gpu-manager-frontend nginx
curl -s http://127.0.0.1:8000/docs | head
curl -s http://127.0.0.1/api/  -o /dev/null -w '%{http_code}\n'   # 首页/SPA
sudo journalctl -u gpu-manager-backend -n 50 --no-pager
```

回滚：把 `deploy/backup/<时间戳>/` 里的原 v1 单元与 nginx 站点拷回对应目录、`daemon-reload`、`enable --now gpu-manager-backend` 等 → v1 恢复（v1 代码/库未被改动）。

### 5.3 启动方式 / How to start & logs

- **开发**：`cd backend && /opt/anaconda3/bin/python -m uvicorn app.main:app --port 8000 --reload`；前端 `npm start`（:3000，CRA 代理 `/api`→:8000）。
- **生产**：`sudo systemctl restart gpu-manager-backend`。
- **日志**：`sudo journalctl -u gpu-manager-backend -f`。

### 5.4 前端重构 / Rebuilding frontend

```bash
source ~/.nvm/nvm.sh && nvm use 18
cd frontend
npm run build        # 常规源码修改无需 npm install；新增依赖才需要
```
构建产物 nginx 直读，`reload`/强刷即可，无需重启后端。CI 校验用 `CI=1 npm run build`。

---

## 6. API 接口 / API Endpoints

> llm 门控：标注 **⚡llm** 的接口要求当前用户 `mode == "llm"`，`traditional` 用户调用返回 **403**（`require_llm_mode`）。标注 **🔒admin** 的接口要求管理员。

### 用户 / Users

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/users/register` | 注册（GPU 配额默认 0；mode = MODE_DEFAULT） |
| POST | `/api/users/login` | 登录 → JWT |
| GET | `/api/users/me` | 当前用户信息（含配额/用量 + **mode**） |
| PUT | `/api/users/password` | 修改密码 |

### 模式 / Mode

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/mode` | 当前用户模式（llm/traditional） |
| PUT | `/api/mode` | 切换当前用户模式（body `{"mode": "llm"|"traditional"}`，非法值 400） |

### GPU

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/gpus/status` | 实时 GPU 状态（NVML，坏卡显示 ERROR 徽章） |
| GET | `/api/admin/allocations` | 🔒admin GPU 分配记录 |

### 容器 / Containers（含 v2 rebuild/protection）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/containers` | 容器列表（user 看自己 / admin 看全部；含 `cleanup_protected`） |
| POST | `/api/containers/start` | 启动容器 |
| DELETE | `/api/containers/{id}` | 停止容器 |
| DELETE | `/api/containers/{id}/remove` | 删除容器（用户自己无权删运行中容器） |
| POST | `/api/containers/{id}/start` | 重启已停止容器 |
| POST | `/api/containers/{id}/rebuild` | 按 removed 容器的配置快照重建（rebuild 能力） |
| GET | `/api/containers/{id}/logs` | 容器日志 |
| PUT | `/api/containers/{id}/protection` | 🔒admin-own/本人：设 `{"protected": true|false}`（入保护池，不再入清理候选） |
| GET | `/api/images` | 预设镜像列表 |
| POST | `/api/admin/images` | 🔒admin 添加预设镜像 |
| DELETE | `/api/admin/images/{id}` | 🔒admin 删除预设镜像 |
| GET | `/api/admin/config/mount-root` | 🔒admin 查看挂载根目录 |

### Agent / 双模式（⚡llm；traditional → 403）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent/session` | 当前会话上下文（历史消息） |
| POST | `/api/agent/chat` | 非流式一问一答（LLM 多轮工具循环后返回整段回复） |
| POST | `/api/agent/chat/stream` | **SSE 流式**：`data:` 帧 `text(delta)` / `tool_use(tool,input)` / `tool_result(tool,ok,result)` / 末尾 `done(reply,tool_trace)` |
| DELETE | `/api/agent/session` | 清空当前会话 |

### 监控 / Monitor（⚡llm）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/monitor/containers` | user 自己 / admin 全部；带 docker 实时运行态 + cleanup_protected |
| GET | `/api/monitor/disk` | 最新 DiskSnapshot + 趋势(24) + 实时水位 + docker 可回收明细 |
| GET | `/api/monitor/candidates` | 🔒admin 当前清理候选（auto/alert 两表 + 决策特征） |

### 管理 / Admin（清理日志 + 告警）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/cleanup-log` | 清理日志（limit 200，倒序） |
| GET | `/api/admin/alerts` | 告警，未解决优先、时间倒序 |
| POST | `/api/admin/alerts/{id}/resolve` | 解决一条告警 |
| GET | `/api/admin/users` | 用户列表（含配额/用量/mode） |
| PUT | `/api/admin/users/{id}/quota` | 设置 GPU 配额 |
| DELETE | `/api/admin/users/{id}` | 删除用户 |
| PUT | `/api/admin/users/reset-password` | 重置密码 |
| GET | `/api/admin/users/check/{username}` | 用户名可用性检查 |

---

## 7. 常见问题 / FAQ

### 7.1 服务启动失败 / Service fails to start
```bash
sudo journalctl -u gpu-manager-backend -n 50 --no-pager
```
常见原因：`WorkingDirectory` 错误、依赖未装、`.env` 格式错、**:8000 被 v1 占用导致 bind 失败**（需先走 §5.2 切换）。

### 7.2 Agent 报错 / 无回复 / LLM 未配置
- `traditional` 模式 → 前端 Agent 页会提示并给切换；直接调 agent API 得 403（预期）。
- `llm` 模式但 `LLM_API_KEY`/`ANTHROPIC_AUTH_TOKEN` 为空 → 请求 LLM 失败。检查 `.env` 后重启后端（`main.py` 在 import 时读 env，模块级 LLMClient 已冻结，**热改 .env 不生效**）。
- 流式卡住/整段一次性出 → 确认走 nginx 且用了本仓库 conf（`proxy_buffering off`）。

### 7.3 清理引擎不动作 / 行为不符预期
- 先确认 `.env`：`CLEANUP_ENABLED`（默认 true）、磁盘水位 `DISK_THRESHOLD` 是否已达。
- 该轮只清理「已停止 + 空闲超 `GRACE_DAYS` + 未保护」容器；运行中容器只进 alert 候选，**永不自动停**。
- dry-run 见 7.5。看日志：`sudo journalctl -u gpu-manager-backend | grep -i cleanup`。

### 7.4 数据迁移 / Database migration
- SQLite→PostgreSQL：设 `DATABASE_URL`，重启自动建表；数据需手动导（代码不迁移数据）。
- **v1 库 → v2 库**：不受支持直接拷贝（v2 schema 多 `User.mode`、`ContainerInstance.cleanup_protected`、以及 `container_events/disk_snapshots/cleanup_logs/chat_messages/admin_alerts` 表）。需要时手动 `INSERT ... SELECT` 迁移 `users`/`container_instances`/`gpu_allocations`/`gpu_images` 并补齐新列默认值，再让 v2 建其余表。**最省事：新库重注册用户 + 重配配额**。

### 7.5 Cleanup dry-run 语义
`CLEANUP_DRY_RUN=true` 时每轮只**记录决策**（日志 action=remove 占位、不删容器不回收空间），用于预演。注意：dry-run 行与真实 remove 一样**计入当日删除上限与冷却**——当天用 dry-run 预演 N 次后再切回真实模式，UTC 零点前可能被限额挡住（设计如此，镜像真实 remove 行以服务日志 UI）。

### 7.6 Nginx 404/500
- 确认 root = `/amax/gpu_manager_v2/frontend/build` 且已构建。
- 确认 `proxy_pass http://127.0.0.1:8000` 后端活着（v1/v2 切换后 :8000 归属变了）。
- `sudo tail -f /var/log/nginx/error.log`。

### 7.7 前端切换 mode 后没生效
前端 ModeContext 每次登录都 `refresh()` 拉服务端 `/api/mode`（权威）；改 `MODE_DEFAULT` 只影响**新注册**用户，存量用户用页面切换或 `PUT /api/mode`。

### 7.8 后端重启
```bash
sudo systemctl restart gpu-manager-backend
```

### 7.9 前端重启

如需重启可选的前端静态服务（直接访问 `:3001` 时使用），构建后执行：

```bash
sudo systemctl restart gpu-manager-frontend
```

生产环境通过 nginx 访问时，更新前端只需重新执行 `npm run build` 并刷新浏览器；nginx 直接读取 `frontend/build`，无需重启前端服务。

---

> 如有其他问题，请联系管理员。
