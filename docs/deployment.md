# GPU Resource Manager — 部署与配置文档

> 版本 2.0 | 2026-06-01

---

## 目录 / Table of Contents

1. [项目概述 / Overview](#1-项目概述--overview)
2. [导航指南 / Navigation Guide](#2-navigation-guide--导航指南)
3. [环境配置 / Environment Config (.env)](#3-环境配置--environment-config-env)
4. [项目结构 / Project Structure](#4-项目结构--project-structure)
5. [部署步骤 / Deployment](#5-部署步骤--deployment)
6. [API 接口 / API Endpoints](#6-api-接口--api-endpoints)
7. [常见问题 / FAQ](#7-常见问题--faq)

---

## 1. 项目概述 / Overview

GPU Manager 是一个基于 Web 的 GPU 资源管理平台，支持多用户 GPU 容器调度、配额管理和实时监控。

Key features:

- Real-time GPU monitoring via NVML
- Multi-tenant GPU allocation with quota enforcement
- Docker container lifecycle management with GPU pass-through
- Role-based access control (user / admin)
- Preset GPU Docker image management
- SSH access into containers

### 1.1 推荐镜像 / Recommended Images
- Ubuntu 20.04 / 22.04 / 24.04
- Debian 11 / 12
- CUDA 官方镜像（Ubuntu 版本）
- PyTorch 官方镜像
- TensorFlow 官方镜像
- 基于 Ubuntu/Debian 构建的 AI 训练镜像

---

## 2. Navigation Guide / 导航指南

本会话中新增或修改的行为，快速索引：

| 变更 | 说明 | 相关章节 |
|------|------|----------|
| **同用户单容器限制** | 同一用户一次只能启动一个容器（必须停止当前容器才能启动新的） | API 容器 |
| **新用户默认 GPU 配额为 0** | 注册后默认无 GPU 权限，需管理员手动分配配额 | FAQ 6.3 |
| **GPU 异常状态显示** | 坏卡不再阻断整个状态查询，显示紫色 `ERROR` 徽章，其他卡不受影响 | FAQ 6.2 |
| **容器挂载根目录可配置** | 宿主机 `gpu-{username}/` 的父目录，通过 `.env` 的 `CONTAINER_MOUNT_ROOT` 设置 | § 2.3 |
| **路径相对化** | Python 代码中所有路径基于 `__file__` 推导，迁移只需改部署配置 | § 4.2 |
| **中英双语文档** | 本文件，覆盖配置/部署/API/FAQ | 全部 |
| **容器停止自动重试** | 停止/删除失败后自动重试 3 次，全部失败才报错，可环境变量调整 | § 2.8 |

---

## 3. 环境配置 / Environment Config (.env)

配置文件位于项目根目录 `./.env`，后端启动时自动加载。
The `.env` file at the project root is loaded automatically on startup.

### 2.1 SECRET_KEY — JWT 签名密钥

**用途 / Purpose**: 签名用户登录令牌，防止令牌伪造。

**获取方式 / How to generate**:

```bash
# 方法一：openssl
openssl rand -hex 32

# 方法二：Python
python3 -c "import secrets; print(secrets.token_hex(32))"
```

**⚠️ 生产环境必须修改！** 使用默认值会导致任意用户可以伪造管理员 token 直接登录。
**Must change in production!** The default value lets anyone forge admin tokens.

**示例 / Example**:
```
SECRET_KEY=080e49fb15d792bda9c089aa8f18e634faef8645bd691d983de757e15fa01648
```

### 2.2 CORS_ORIGINS — 跨域来源

**用途 / Purpose**: 允许访问后端的域名列表，逗号分隔。浏览器会拦截不在列表中的跨域请求。

**默认 / Default**:
```
CORS_ORIGINS=http://localhost:3000,http://localhost,http://127.0.0.1
```

如果需要通过其他域名访问，将域名加入列表。生产环境建议设为具体域名而非 `*`。
Add your production domain here if accessing from a different origin.

### 2.3 CONTAINER_MOUNT_ROOT — 容器挂载根目录

**用途 / Purpose**: 容器在宿主机上的工作目录的父路径。启动容器时会在该目录下创建 `gpu-{username}/` 子目录，挂载到容器的 `/workspace`。

**默认 / Default**:
```
CONTAINER_MOUNT_ROOT=/amax
```

以用户 `XM` 为例，效果为：
```
宿主机: /amax/gpu-XM/  →  容器内: /workspace/
```

**注意 / Note**: Docker bind mount 必须使用绝对路径，此处不支持相对路径。

### 2.4 ALLOCATION_TIMEOUT — GPU 分配锁超时

**用途 / Purpose**: GPU 分配请求等待锁的超时时间（秒）。多个用户同时申请 GPU 时串行排队，超时后返回 503。

**默认 / Default**:
```
ALLOCATION_TIMEOUT=60
```

### 2.5 PORT_RANGE — 容器 SSH 端口范围

**用途 / Purpose**: 容器 SSH 服务映射到宿主机的端口范围。端口用完后新容器无法启动。

**默认 / Default**:
```
PORT_RANGE_START=22000
PORT_RANGE_END=22999
```

### 2.6 STALE_MEMORY_THRESHOLD — GPU 悬空判断阈值

**用途 / Purpose**: 显存利用率低于此百分比时 GPU 被认为处于空闲状态，用于检测"分配了但容器已停止"的悬空 GPU。

**默认 / Default**:
```
STALE_MEMORY_THRESHOLD=5
```

### 2.7 DATABASE_URL — 数据库连接

**用途 / Purpose**: 数据库连接字符串。默认使用 SQLite，支持切换为 PostgreSQL。

**默认 / Default** (SQLite):
```
DATABASE_URL=sqlite:///./gpu_resource_manager.db
```

**PostgreSQL 示例 / Example**:
```
DATABASE_URL=postgresql://user:password@host:5432/gpu_manager
```

### 2.8 DOCKER_STOP_* — 容器停止重试

**用途 / Purpose**: 停止/删除容器时，`docker stop` 失败后自动重试的参数。偶发失败（进程响应慢、daemon 抖动）时重试可提高成功率，避免直接报 500。

| 变量 | 默认 | 说明 |
|------|------|------|
| `DOCKER_STOP_ATTEMPTS` | `3` | 停止总尝试次数（含首次） |
| `DOCKER_STOP_TIMEOUT` | `10` | 每次尝试的 stop 超时（秒） |
| `DOCKER_STOP_RETRY_DELAY` | `2` | 重试间隔基数（秒），第 n 次间隔 = `2 × n` |

**默认 / Default**:
```
DOCKER_STOP_ATTEMPTS=3
DOCKER_STOP_TIMEOUT=10
DOCKER_STOP_RETRY_DELAY=2
```

---

## 3. 项目结构 / Project Structure

```
{PROJECT_ROOT}/
├── .env                          # 环境配置（本文件）
├── backend/                      # Python FastAPI 后端
│   ├── app/
│   │   ├── main.py               # 应用入口，CORS，启动事件，.env 加载
│   │   ├── database.py           # SQLAlchemy 引擎 + get_db 依赖
│   │   ├── models.py             # ORM 模型（User, ContainerInstance, GpuAllocation, ...）
│   │   ├── schemas.py            # Pydantic 请求/响应模型
│   │   ├── auth.py               # JWT 认证 + get_current_user / get_current_admin 依赖
│   │   ├── routers/
│   │   │   ├── users.py          # 用户注册/登录/密码管理 + admin 用户管理
│   │   │   ├── gpus.py           # GPU 状态查询 + admin 分配记录
│   │   │   └── containers.py     # 容器生命周期 + 镜像管理 + 系统配置
│   │   ├── services/
│   │   │   ├── gpu_monitor.py    # NVML GPU 只读状态查询（单例）
│   │   │   ├── gpu_allocator.py  # GPU 分配/释放/配额检查（线程锁）
│   │   │   └── docker_runner.py  # Docker 容器操作 + SSH 配置
│   │   └── crud/
│   │       ├── users.py          # 用户 CRUD + 密码哈希
│   │       └── containers.py     # 容器/配额/分配/镜像 CRUD
│   ├── requirements.txt
│   └── gpu_resource_manager.db   # SQLite 数据库文件
├── frontend/                     # React SPA 前端
│   ├── src/
│   │   ├── App.js                # 路由配置
│   │   ├── pages/
│   │   │   ├── Login.js          # 登录/注册
│   │   │   ├── Home.js           # 首页 + GPU 状态图例
│   │   │   ├── Dashboard.js      # GPU 状态看板（实时刷新 5s）
│   │   │   ├── ContainerManagement.js  # 容器管理（启动/停止/删除）
│   │   │   ├── UserProfile.js    # 用户信息 + 密码修改 + admin 重置密码
│   │   │   ├── AdminUsers.js     # 用户列表 + 配额管理
│   │   │   └── AdminImages.js    # 镜像管理 + 挂载根目录显示
│   │   └── services/
│   │       └── api.js            # Axios 实例 + JWT 拦截器
│   └── build/                    # 构建产物（由 npm run build 生成）
├── deploy/
│   ├── gpu-manager-backend.service   # systemd 后端服务
│   ├── gpu-manager-frontend.service  # systemd 前端服务
│   ├── gpu-manager.nginx.conf        # Nginx 反向代理配置
│   └── install-services.sh           # 一键安装脚本
└── docs/
    └── deployment.md              # 本文档
```

---

## 4. 部署步骤 / Deployment

### 4.1 首次部署 / First-time setup

以下命令默认在项目根目录下执行（如 `/amax/gpu_manager/`）。
All commands below assume you are in the project root directory.

```bash
# 1. 安装 Python 依赖
cd ./backend
pip install -r requirements.txt

# 2. 安装前端依赖并构建（Node.js 16+）
cd ./frontend
npm install
npm run build

# 3. 配置 .env（参考第 2 章）
# 至少修改 SECRET_KEY

# 4. 配置 Nginx
sudo cp ./deploy/gpu-manager.nginx.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/gpu-manager.nginx.conf /etc/nginx/sites-enabled/
# ⚠ 检查 nginx root 路径是否与项目实际位置一致
sudo nginx -t && sudo systemctl reload nginx

# 5. 配置 systemd 服务
sudo cp ./deploy/gpu-manager-backend.service /etc/systemd/system/
# ⚠ 检查 WorkingDirectory 和 ExecStart 路径
sudo systemctl daemon-reload
sudo systemctl enable --now gpu-manager-backend
```

### 4.2 迁移到新目录 / Migrating to a new path

由于 Python 代码中的所有路径都基于 `__file__` 动态推导，迁移时**无需修改代码**。只需调整：

| 需要手动修改 | 原因 |
|-------------|------|
| `deploy/gpu-manager-backend.service` 的 `WorkingDirectory` 和 `ExecStart` | systemd 配置 |
| `deploy/gpu-manager.nginx.conf` 的 `root` | Nginx 静态文件路径 |
| `.env` 的 `CONTAINER_MOUNT_ROOT`（如果需要） | 容器挂载目录 |

### 4.3 启动方式 / How to start

**开发 / Development**:

启动后端：
```bash
cd ./backend
/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动前端开发服务器（需要 Node.js 16+）：
```bash
cd ./frontend
npm start
# 默认运行在 http://localhost:3000，自动代理 API 到后端
```

**生产 / Production (systemd)**:
```bash
# ⚠ 首次从手动进程切换到 systemd 时，先杀掉手动进程（防止8000端口被手动进程占用）
# lsof -i :8000 -t | xargs kill -9
# （之后只需下面这行，不再需要手动启动）

sudo systemctl restart gpu-manager-backend
sudo systemctl status gpu-manager-backend
```

**查看日志 / Logs**:
```bash
journalctl -u gpu-manager-backend -f
```

### 4.4 前端重构 / Rebuilding Frontend

修改 `frontend/src/` 下的源码后需要重新构建才会生效。

**前提：Node.js 16+**（当前系统为 Node 10，需通过 nvm 切换）：
```bash
source ~/.nvm/nvm.sh && nvm use 18
```

**构建**：
```bash
cd ./frontend
npm run build
```

构建产物输出到 `frontend/build/`，Nginx 直接读取磁盘文件，**无需重启服务**。
刷新浏览器（Ctrl+F5 强刷）即可看到更新。

> ⚠ 如果新增了 npm 依赖才需要先执行 `npm install`，常规源码修改只需要 `npm run build`。


---

## 5. API 接口 / API Endpoints

### 认证 / Auth

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/users/register` | 注册新用户（默认 GPU 配额为 0） |
| POST | `/api/users/login` | 登录，返回 JWT |
| GET | `/api/users/me` | 获取当前用户信息（含配额和用量） |

### GPU

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/gpus/status` | 实时 GPU 状态（NVML） |

### 容器 / Containers

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/containers` | 查看容器列表（用户看自己，admin 看全部） |
| POST | `/api/containers/start` | 启动容器 |
| DELETE | `/api/containers/{id}` | 停止容器 |
| DELETE | `/api/containers/{id}/remove` | 删除容器 |
| POST | `/api/containers/{id}/start` | 重启已停止的容器 |

### 管理 / Admin

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 用户列表 |
| PUT | `/api/admin/users/{id}/quota` | 设置 GPU 配额 |
| GET | `/api/admin/allocations` | GPU 分配记录 |
| POST | `/api/admin/images` | 添加预设镜像 |
| DELETE | `/api/admin/images/{id}` | 删除预设镜像 |
| GET | `/api/admin/config/mount-root` | 查看容器挂载根目录 |

---

## 6. 常见问题 / FAQ

### 6.1 服务启动失败 / Service fails to start

检查日志：
```bash
journalctl -u gpu-manager-backend -n 50 --no-pager
```

常见原因：
- `WorkingDirectory` 路径错误（迁移后）
- Python 依赖未安装
- `.env` 文件格式错误

### 6.2 GPU 状态显示异常 / GPU status error

GPU 卡故障时前端会显示紫色 `ERROR` 徽章，表示该 GPU 不可用。这是正常行为，无需操作。
硬件恢复后重启后端即可恢复正常监测。

### 6.3 用户无法启动容器 / User cannot start container

可能原因：
- GPU 配额为 0（需 admin 在用户管理页面设置配额）
- 已有运行中的容器（需先停止）
- GPU 资源不足
- 预设镜像不存在

### 6.4 数据库迁移 / Database migration

如需从 SQLite 切换到 PostgreSQL：
1. 在 `.env` 中设置 `DATABASE_URL`
2. 重启后端，FastAPI 会自动创建表
3. 数据不会自动迁移，需手动导出导入

### 6.5 Nginx 返回 500 或 404

- 确认 `root` 路径指向正确的 `frontend/build` 目录
- 确认 `proxy_pass` 指向正确的后端地址
- 检查 nginx 错误日志：`/var/log/nginx/error.log`

---

> 如有其他问题，请联系管理员。
