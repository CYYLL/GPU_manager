# GPU Resource Manager v2 部署与运维

本文说明部署顺序、权限边界和日常维护。具体环境变量及可选项以项目根目录的 `.env.example` 为准；服务定义和反向代理规则以 `deploy/` 中的文件为准；接口参数与响应以运行中的 API 文档为准。

## 1. 组成与前提

- 后端提供认证、GPU 与容器管理、Agent、监控和清理；需要能访问 Docker 与 GPU 驱动。
- 前端是 React 构建产物，生产环境由 nginx 提供页面并转发 API。后端的 8000 端口只监听本机，外部访问统一走 nginx。可选的前端 systemd 服务提供独立静态访问。
- 数据库保存用户、配额、预设镜像、容器快照、事件、聊天与告警；Docker 管理镜像和容器层；用户工作区位于单独的宿主机挂载目录。

| 称谓 | 所属范围 | 用途 |
|------|----------|------|
| 系统管理员账号 | Linux，具有 `sudo` 权限 | 安装依赖、配置 systemd/nginx、启动服务 |
| 服务运行用户（`DEPLOY_USER`） | Linux，模板默认 `amax` | 运行后端及独立前端，访问 Docker；不要求拥有 `sudo` 权限 |
| 平台管理员账号 | GPU Resource Manager 数据库，角色为 `admin` | 登录网页管理用户、镜像、配额等；与 Linux 账号相互独立 |

平台管理员不会因此获得 Linux 的 `sudo` 权限；系统管理员也不会自动成为平台管理员。

### 服务器依赖

| 依赖 | 用途与要求 | 检查方式 |
|------|------------|----------|
| Linux、systemd、bash | 安装脚本使用 `systemctl` 管理服务；安装时需要 `sudo` 权限 | `systemctl --version` |
| NVIDIA GPU 驱动与 NVML | 读取 GPU 状态和显存 | `nvidia-smi` |
| Docker Engine、NVIDIA Container Toolkit | 管理镜像和容器，并让容器使用 GPU；后端运行用户须能访问 Docker | `docker info`，并以运行用户检查 Docker 权限 |
| Python、pip | 在后端服务 `ExecStart` 所用环境中安装 `backend/requirements.txt`，包括 FastAPI、Uvicorn、SQLAlchemy、Docker SDK、pynvml 和模型 SDK | `python --version`、`python -m pip --version` |
| Node.js、npm | 前端由 `frontend/package.json` 和锁文件安装并构建；现有独立前端服务使用 Node 18，首次执行 `npx serve` 可能需要下载 | `node --version`、`npm --version` |
| nginx | 对外提供页面并将 `/api` 代理到本机后端 | `nginx -v` |
| 网络访问 | 安装时下载 Python/npm 依赖；运行时按需连接模型接口及 Docker Hub | 在新服务器检查相应目标的连通性 |

### 版本参考

服务器的驱动和系统组件应与其硬件及发行版匹配

| 组件 | 现有服务器版本 | 项目中的版本依据 |
|------|----------------|------------------|
| 操作系统 | Ubuntu 20.04.1 LTS | 未锁定；部署脚本使用 systemd 和 Ubuntu 示例命令 |
| NVIDIA 驱动 | 525.89.02 | 未锁定；以新服务器 GPU 及系统兼容性为准 |
| Docker Engine / NVIDIA Container Toolkit | 20.10.5 / 1.18.2 | 未锁定；按各自官方安装说明选择 |
| Python / Node.js / npm / nginx | 3.8.5 / 18.20.4 / 10.7.0 / 1.18.0 | 服务模板使用 Node 18；其余未锁定 |
| 后端 Python 包 | 见 `backend/requirements.txt` | 多数直接依赖固定版本；`anthropic`、`openai` 使用版本范围，间接依赖未锁定 |
| 前端 npm 包 | 见 `frontend/package-lock.json` | `npm ci` 按锁文件安装；`frontend/package.json` 声明直接依赖 |

先确认目标机器没有与待安装服务冲突的旧实例

### 依赖安装与检查（Ubuntu 示例）

以下用于全新服务器；已安装的组件直接跳过其安装步骤。GPU 驱动需按显卡和系统版本选择，请先按 [NVIDIA 驱动说明](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/ubuntu.html)安装，并执行 `nvidia-smi` 确认正常。

**1. 安装基础包和 Docker。** 全新 Ubuntu 服务器可使用 [Ubuntu 提供的 `docker.io` 包](https://ubuntu.com/server/docs/how-to/containers/docker-for-system-admins/)；如需 Docker CE，改按 [Docker 官方安装说明](https://docs.docker.com/engine/install/ubuntu/)操作，不混用两种软件源。

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx curl gnupg git docker.io
sudo systemctl enable --now docker nginx
```

**2. 安装 NVIDIA Container Toolkit 并配置 Docker。** 这一步要求 GPU 驱动和 Docker 已安装；命令来源见 [NVIDIA 安装说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)。

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**3. 检查服务和运行用户。** `DEPLOY_USER` 是服务运行用户；更换用户只需修改赋值。使用独立前端服务时，还需按 [nvm 说明](https://github.com/nvm-sh/nvm#installing-and-updating)为该用户安装 nvm。

```bash
DEPLOY_USER=amax
id "$DEPLOY_USER" || sudo useradd -m -s /bin/bash "$DEPLOY_USER"
nvidia-smi
sudo usermod -aG docker "$DEPLOY_USER"
sudo -u "$DEPLOY_USER" docker info >/dev/null
```

安装 nvm 后，以服务运行用户完成独立前端服务所需的 Node 环境检查：

```bash
DEPLOY_USER=amax
sudo -iu "$DEPLOY_USER"
nvm install 18
nvm use 18
node --version
npm --version
exit
```

## 2. 配置与数据

从 `.env.example` 建立项目根目录的 `.env`，按实际环境检查以下类别：

| 类别 | 需要确认的内容 |
|------|----------------|
| 安全与访问 | 登录签名密钥、令牌有效期、跨域来源、容器访问地址 |
| 存储与资源 | 数据库、工作区根目录、GPU 分配、容器端口及 Docker 停止策略 |
| Agent | 用户默认模式、模型提供方、凭据、模型、流式输出 |
| 监控与清理 | 采样与保留、清理阈值及限额、预演、闲置 GPU 自动停止 |

后端启动时读取 `.env`；修改后需重启后端。进程环境变量优先于 `.env`，因此调整配置前应检查 systemd 单元。前端构建时使用的变量需重新构建才会生效。不要把真实凭据提交到仓库。

数据库表会在后端启动时创建，但项目没有自动数据库迁移工具。修改模型或切换数据库前应备份并评估数据迁移。旧版数据库不能直接作为新版数据库使用；切换脚本也不会迁移用户、配额或容器记录。

## 3. 安装与切换

安装脚本不会下载或复制项目。新服务器应先将项目放在模板所用目录；如需更换位置，按表同步修改配置。

| 内容 | 当前模板位置 | 更换项目目录时 |
|------|--------------|----------------|
| 项目代码 | `/amax/gpu_manager_v2/` | 自行放置项目，并修改下列引用 |
| `.env`、前端构建产物 | 项目根目录下的 `.env`、`frontend/build/` | 随项目移动；后端从代码位置读取 `.env` |
| 默认 SQLite 数据库 | 项目下的 `backend/gpu_resource_manager.db` | 修改后端服务的 `WorkingDirectory`；也可用 `DATABASE_URL` 单独指定 |
| 容器工作区 | `.env` 的 `CONTAINER_MOUNT_ROOT`，模板为 `/amax/` | 独立设置，不随项目移动 |
| Docker 镜像与容器数据 | Docker 数据目录 | 独立于项目；以 Docker 实际配置为准 |
| 后端 systemd 服务 | 模板 `deploy/gpu-manager-backend.service`；安装到 `/etc/systemd/system/` | 修改 `User`、`WorkingDirectory` 和 `ExecStart`；当前可执行文件为 `/home/amax/.local/bin/uvicorn` |
| 独立前端 systemd 服务 | 模板 `deploy/gpu-manager-frontend.service`；安装到 `/etc/systemd/system/` | 修改 `User`、Node 环境及 `ExecStart` 中的构建目录；安装脚本会启动此服务 |
| nginx 站点 | 模板 `deploy/gpu-manager.nginx.conf`；安装到 `/etc/nginx/sites-available/` | 修改 `root` 指向新的 `frontend/build/`；API 仍代理到本机 8000 |

全新服务器只使用安装脚本，不运行旧版切换脚本。先完成上一节的依赖安装与检查。

将项目复制到 `/amax/gpu_manager_v2/` 后，由系统管理员账号设置归属，再以服务运行用户安装依赖和构建前端：

```bash
DEPLOY_USER=amax
cd /amax/gpu_manager_v2
sudo chown -R "$DEPLOY_USER:$DEPLOY_USER" /amax/gpu_manager_v2
sudo -iu "$DEPLOY_USER"
cd /amax/gpu_manager_v2
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
(cd frontend && npm ci && npm run build)
exit
DEPLOY_HOME=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
sudo -u "$DEPLOY_USER" sed -i "s/^User=amax$/User=$DEPLOY_USER/" deploy/gpu-manager-backend.service deploy/gpu-manager-frontend.service
sudo -u "$DEPLOY_USER" sed -i "s#^Environment=\"NVM_DIR=/home/amax/.nvm\"#Environment=\"NVM_DIR=$DEPLOY_HOME/.nvm\"#" deploy/gpu-manager-frontend.service
sudo -u "$DEPLOY_USER" sed -i 's#^ExecStart=/home/amax/.local/bin/uvicorn#ExecStart=/amax/gpu_manager_v2/.venv/bin/uvicorn#' deploy/gpu-manager-backend.service
grep -E '^(User|Environment|ExecStart)=' deploy/gpu-manager-{backend,frontend}.service
```

上述命令使用项目内的 Python 虚拟环境，并同步修改服务用户、nvm 路径及后端启动路径。若项目目录不同，先按上表修改命令及服务模板。编辑 `.env` 后，由系统管理员账号安装服务：

```bash
sudo bash /amax/gpu_manager_v2/deploy/install-services.sh
```

安装脚本会检查前端构建产物和 `.env`，安装 systemd 单元，并配置 nginx。新数据库的首个注册用户会成为平台管理员，应先在受控访问下完成注册，再向其他用户开放 nginx 入口。后端 8000 端口保持本机监听；需要从外部 SSH 进入容器时，另按 `.env` 中的容器端口范围配置防火墙。

若检测到旧版服务正在使用目标入口，脚本会拒绝直接覆盖。旧版切换应安排维护窗口，先备份数据，再运行：

```bash
sudo bash deploy/gpu-manager-v2-cutover.sh
```

切换脚本会备份原有服务与 nginx 文件、停止旧服务、安装新版服务并检查状态。切换后需单独完成用户及数据迁移或重新初始化；旧版代码和数据库不会由脚本删除。需要回滚时，从 `deploy/backup/` 中对应的备份恢复服务与 nginx 文件，重新加载 systemd 并启动旧服务，同时核对原数据库。回滚不会自动合并切换后产生的数据。

已有部署更新后端监听地址前，先确认外部设备可以访问 nginx 的入口端口，并已放行相应防火墙规则。随后重新安装服务单元并重启后端：

```bash
cd /amax/gpu_manager_v2
sudo install -m 644 deploy/gpu-manager-backend.service /etc/systemd/system/gpu-manager-backend.service
sudo systemctl daemon-reload
sudo systemctl restart gpu-manager-backend
```

用 `ss -ltnp '( sport = :8000 )'` 检查 8000 端口只绑定 `127.0.0.1`；从外部设备通过 nginx 入口验证页面和 API。外部设备不再直接访问 8000 端口。

## 4. 验证与更新

部署后检查服务状态、首页、API 文档和后端日志；再用平台普通用户与平台管理员账号分别验证登录、GPU 看板、容器列表和权限。LLM 模式还应验证 Agent 的流式回复。

```bash
sudo systemctl is-active gpu-manager-backend gpu-manager-frontend nginx
sudo nginx -t
ss -ltn '( sport = :8000 or sport = :80 )'
curl -fsS -o /dev/null http://127.0.0.1/
curl -fsS http://127.0.0.1/api/host-ip
sudo journalctl -u gpu-manager-backend -n 50 --no-pager
```

### 日志查看

后端异常（包括模型 API 连接失败、接口报错和 Agent 工具异常）写入 systemd journal。实时跟踪与回看历史分别使用：

```bash
sudo journalctl -u gpu-manager-backend -f
sudo journalctl -u gpu-manager-backend --since "1 hour ago" --no-pager
```

排查页面或容器相关问题时，按需查看对应服务；独立前端服务为可选项，nginx 错误日志路径以运行中的 nginx 配置为准：

```bash
sudo journalctl -u gpu-manager-frontend -n 50 --no-pager
sudo journalctl -u docker -n 50 --no-pager
sudo tail -n 50 /var/log/nginx/error.log
```

聊天回复和工具执行是否成功会进入数据库会话记录，模型 API 的完整异常堆栈只在后端服务日志中；清理记录和告警可在平台管理员的“清理与告警”页查看，容器内应用输出用 `docker logs` 查看。流式接口即使返回 HTTP 200，也应检查最终回复和后端异常日志。

后端代码或 `.env` 变更后重启后端：

```bash
sudo systemctl restart gpu-manager-backend
```

前端源码变更后重新构建并刷新浏览器：

```bash
(cd frontend && npm run build)
```

nginx 直接读取前端构建产物，常规前端更新无需重启后端或 nginx。使用可选的独立前端服务时，构建后执行：

```bash
sudo systemctl restart gpu-manager-frontend
```

更改 nginx 配置前先校验配置，再重新加载 nginx。

## 5. 功能与权限

| 功能 | 行为与权限 |
|------|------------|
| 双模式 | 用户模式由服务端保存；传统模式使用手动页面，LLM 模式使用 Agent。Agent 与相关监控接口受模式限制。 |
| GPU 与容器 | 按配额分配 GPU；用户管理自己的容器，平台管理员可查看和管理授权范围内的容器。停止保留容器，删除容器保留工作区；清理形成的快照可重建。 |
| 容器访问 | 后端提供宿主机访问地址和映射端口；地址获取失败时 Agent 不会编造连接命令。 |
| 镜像 | 平台管理员可维护预设、搜索 Docker Hub 候选、在明确选择后拉取镜像。拉取进度显示在对话中；失败会返回原因。所有 LLM 模式用户可查询本地预设镜像的系统、架构及可读取的软件包摘要。 |
| 删除本地镜像 | 仅平台管理员可执行；若运行中或已停止的容器使用镜像，会警告并拒绝。仅删除预设记录是单独操作。 |
| 监控与告警 | 后端定时采样并保存事件与磁盘快照；平台管理员可查看清理日志和容量告警。 |

镜像检查不会启动镜像程序，其软件包摘要不是完整清单，也不包含容器运行后生成的文件。镜像拉取任务保存在后端进程内：离开对话不会自动停止后台拉取，但重启后端会中断任务；重新拉取时 Docker 可复用已保存的完整镜像层。

## 6. 自动清理与闲置停止

自动清理只考虑超过宽限期、已停止且未受清理保护的容器。它会重新检查容器状态后才执行删除，并保留工作区；运行中的容器不会被自动清理。清理受轮次、每日上限、冷却和互斥锁约束。镜像及构建缓存按各自条件回收；空间不足或回收效果低时产生平台管理员告警。

清理预演只记录决策，不释放空间；预演记录仍会影响当日上限与冷却。清理保护不豁免闲置 GPU 自动停止：后者只处理持续空闲的运行中容器，停止前会重新检查 GPU 活动，并保留容器和工作区。

## 7. 排查顺序

| 现象 | 先检查 |
|------|--------|
| 服务无法启动 | 后端日志、依赖、`.env`、Docker 权限、目标入口是否被其他服务占用。 |
| 页面或 API 异常 | 前端构建产物、nginx 配置及日志、后端状态；流式响应还需检查代理缓冲。 |
| Agent 无回复 | 当前用户模式、模型凭据及网络、后端日志；修改模型配置后重启后端。 |
| 镜像搜索或拉取失败 | Docker Hub 连通性、DNS、代理、Docker 服务和任务返回信息。 |
| 清理未执行 | 开关、预演、阈值、候选容器、保护状态、宽限期、每日上限与冷却。 |
| 容量告警 | 对照清理日志、Docker 可回收空间及工作区占用；历史告警不会因代码更新而自动改写。 |

接口清单、请求格式和响应字段请查看运行中后端提供的 API 文档；不要依赖本文档中的操作说明推断接口参数。
