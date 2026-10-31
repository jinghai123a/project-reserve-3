# 云端常驻部署

这套配置把现有 CloudKit 的 Git 状态机、Redis/RQ 唤醒桥和真实 Codex CLI 放到一台常驻 Linux 云主机。Git 控制分支仍是任务真相源，没有新增任务数据库。这里交付的是可构建部署包；当前编辑环境没有 Docker、云主机登录及模型认证，不能据此声称云端已经上线。

## 组件和职责

| 服务 | 实际工作 | 健康与成功含义 |
| --- | --- | --- |
| `redis` | AOF 持久化传递唤醒事件，512 MB 上限，不淘汰任务 | PING 只证明传输可用 |
| `worker` | 执行现有 `cloudkit.runtime.queue_bridge worker --poll`；每轮按原监督器做真实模型探针、独立验收和 Git 检查点 | 容器健康只证明消费者进程和 Redis 存活 |
| `dispatcher` | 每 60 秒推进已审核的公司任务图，再用现有 `continuity.pending()` 找到到期任务，检查租约后补齐 Git outbox 并交给现有 RQ 桥 | 心跳只证明最近有成功扫描；没有模型调用 |
| `init` | 首次创建持久目录并设置 UID/GID 1000 权限 | 一次性基础设施准备，不接收任务 |

`dispatcher` 覆盖未来到期的 `RETRY_WAIT`、后来注册的任务和 Redis 重启后丢失的投递。它不复活 `DONE`、`BLOCKED`、`CANCELLED` 或 `WAITING_CAPABILITY`，不绕过租约。连续三次传输异常退出，Compose 最多重启三次，避免永久快速重试。`worker` 也最多故障重启三次；异常归因见容器日志和 Git 任务证据。

只运行一组 worker/dispatcher。任务内部可使用 Codex 原生专业子代理，独立文件写入所有权和验收由项目规则负责；不要用 `--scale worker=5` 伪装成五个部门。两个服务共享同一控制工作副本，当前配置针对单租户可信项目。

## 云主机准备

需要可持久化磁盘的 Linux 主机、Docker Engine、Docker Compose v2.24 或更新版本、至少约 2 vCPU / 8 GB RAM，以及 GitHub、npm/PyPI、Docker Hub 和实际模型端点的正常网络。没有硬编码公网入口，Redis不映射宿主机端口，默认无 Web 面板。不要把不可信外部容器接入 `cloudkit` 网络。

在云主机克隆本代码仓库，然后执行：

```bash
cp deploy/.env.example deploy/.env
install -d -m 700 deploy/secrets
install -m 600 /dev/null deploy/secrets/github_token
```

把已有 GitHub fine-grained token 通过云端密钥管理器写入 `deploy/secrets/github_token`。仅授权业务仓库和控制仓库需要的 Contents 读写；若启用现有 GitHub Actions 后备唤醒，再授予控制仓库 Actions 读写。不要把 token 放进聊天、仓库、clone URL 或 `.env`。Docker Compose 文件型 secret 按宿主机权限挂载，执行以下命令使容器 UID 1000 可读取：

```bash
sudo chown 1000:1000 deploy/secrets/github_token
sudo chmod 600 deploy/secrets/github_token
```

`.env.example` 的两个仓库身份刻意留空，启动前必须明确填写；Compose 缺少任一值会直接报错，运行时也会在任何 Git 操作前检查两项身份。公司托管模板采用一个业务仓库保存业务代码和自己的 `automation/state`：

- `CLOUDKIT_CONTROL_REPOSITORY`：填写实际业务仓库的 `owner/repository`，用于该业务的 `automation/state`。
- `CLOUDKIT_PROJECT_REPOSITORY`：填写同一个实际业务仓库的 `owner/repository`，用于工作代码。不要把总部仓、规则仓或运行时仓当作客户业务仓或业务状态仓。
- 每个业务使用不同的 `COMPOSE_PROJECT_NAME`，隔离数据卷和服务。底层通用运行时仍支持明确配置的独立控制仓，但公司托管模板不采用共享公司仓存储客户任务的方式；切换配置不会自动迁移已有状态。
- `CLOUDKIT_SOURCE_SHA` 留空会在准备阶段读取业务分支当前完整 SHA；固定复现时填完整 SHA。已有控制状态的业务仓库/分支若不同则拒绝启动，不会擅自把原任务迁移到另一项目。
- `CLOUDKIT_MODEL` 留空使用已认证 Codex 的模型设置，不猜测账户拥有哪个模型。
- `CLOUDKIT_GITHUB_TOKEN_FILE` 相对路径按仓库根目录解释，也可使用云主机绝对路径。

首次模型认证使用官方设备登录，发生在云端持久卷：

```bash
bash deploy/cloudkit.sh login
```

此命令构建镜像，创建卷并运行 `codex login --device-auth`。账户所有者按官方页面完成一次认证；脚本不自动更换账号、复制本地登录、不改用 API 付费，也不操作付款信息。登录缓存只保存在 `cloudkit_data` 的 `home/.codex` 中。

## 一键启动与验证

凭据准备完成后：

```bash
bash deploy/cloudkit.sh up
```

实际执行顺序：Compose 校验 → 构建固定版本镜像 → 初始化持久目录 → 等待 Redis 健康 → 克隆控制库并幂等初始化 `automation/state` → 用真实 Codex 返回一次随机 nonce 证明 → 启动 worker 和 dispatcher 并等待健康。

缺少 token、控制仓库不可读写、业务分支不存在、Codex 未认证、实际模型调用失败时，命令返回非零；不会用 fixture 或假响应替代。真实探针会消耗正常模型额度。失败时已创建的卷及 Git 状态保留，Redis可能继续运行，修复后再次 `up` 即可。

```bash
bash deploy/cloudkit.sh status
bash deploy/cloudkit.sh logs
bash deploy/cloudkit.sh ready
```

`ready` 会重新进行一次真实模型探针，再检查 Git 和 Redis；它与容器健康检查不同。启动要求最近 24 小时的成功探针。长时间运行不靠此收据宣称永久就绪，每个任务仍执行监督器自身的能力验证。容器健康不代表业务 DONE，业务 DONE 必须由控制分支任务状态和独立验收证据证明。

## 原生专业部门配置

首次 `up` 从 `CLOUDKIT_AGENT_CONFIG_SOURCE` 读取 `config.toml` 和 `agents/*.toml`，只导入 agents/features 顶层配置及对应角色文件，保存到 `CODEX_HOME`。默认来源是 `/data/control/.codex`，保留原行为：默认目录没有 `config.toml` 时跳过导入。自定义来源必须存在且包含 `config.toml`，否则启动失败，不能以缺少角色配置的状态继续。登录缓存完整保留。发现现有不同设置就报错并保留原文件，不静默覆盖模型、provider、权限或认证。

总部生成的运行包可在构建镜像前把审核后的固定角色配置放入 `deploy/company-rules/.codex`，再在该项目的 `deploy/.env` 中设置：

```dotenv
CLOUDKIT_AGENT_CONFIG_SOURCE=/opt/cloudkit/deploy/company-rules/.codex
```

该路径指向镜像内文件；现有 Dockerfile 会复制 `deploy` 目录。角色配置因此可与业务控制状态分离，无需把总部规则复制到每个业务仓库。公司托管项目将 `CLOUDKIT_CONTROL_REPOSITORY` 与 `CLOUDKIT_PROJECT_REPOSITORY` 设置为同一业务仓库，并使用独立的 `COMPOSE_PROJECT_NAME`，使任务状态、数据卷和服务分别归属该项目。已有卷仍会校验项目身份，不能借更换配置来源迁移旧任务。

如需保留已有个人配置，在 `.env` 增加 `CLOUDKIT_ACTIVATE_CONTROL_CONFIG=0`。这会关闭上述来源的自动导入；之后由配置负责人把角色表合并进已有 Codex 配置，再做真实探针。每次 `up` 先确认控制工作树干净并执行快进拉取；分叉或本地未提交修改会报错保留现场。规则更新后可执行以下命令，按当前配置来源重新准备，再检查/合并配置；它不会破坏本地不同的配置。镜像内规则更新需要先重新构建对应运行包：

```bash
bash deploy/cloudkit.sh exec git -C /data/control pull --ff-only
bash deploy/cloudkit.sh exec python /opt/cloudkit/deploy/runtime.py prepare
bash deploy/cloudkit.sh ready
```

角色文件使原生 Codex 能按专业分派；它不证明此次实际启动了子代理。真实任务的日志、交接物和验收证据才是实际运行证明。

## 提交任务

跨部门项目使用 [公司协调器](../docs/company.zh-CN.md) 注册完整任务图、记录独立审核，并首次绑定 `/data/runs` 下的持久项目 checkout。之后本 dispatcher 自动推进依赖与交接；单个方案不满足条件只记录该目标的阻塞。共享路径同一时刻只有一个顶层 worker，原生专业子代理按需并行。以下单任务入口继续兼容。

任务负责人把已审阅的任务计划放进控制仓库，例如 `plans/project-task.json`。计划使用现有监督器 schema：精确 `project_id/branch/source_sha`、业务目标、真实 Codex backend、独立 acceptance argv 数组。不要把 fixture backend 当生产模型。容器内 Python 路径为 `/opt/venv/bin/python`，真实适配器命令示例：

```json
["/opt/venv/bin/python", "-m", "cloudkit.runtime.codex_worker_backend", "--timeout", "600"]
```

`command` 与 `probe_command` 都可使用该适配器，监督器会分别传入 work/capability 请求；需要指定模型时在两个 argv 中同时增加 `--model` 和实际可用模型 ID。已有业务验收命令保持独立，不让模型自己决定 PASS。

计划提交到控制库后：

```bash
bash deploy/cloudkit.sh exec git -C /data/control pull --ff-only
bash deploy/cloudkit.sh exec python -m cloudkit.runtime.worker_supervisor \
  --repository /data/control register --task-id project-task \
  --plan /data/control/plans/project-task.json
bash deploy/cloudkit.sh exec python -m cloudkit.runtime.queue_bridge \
  --repository /data/control inspect --task-id project-task
```

注册后 60 秒内由到期扫描投递。`plans/project-task.json` 是具体项目负责人提交的真实计划路径，不是包内自带的测试任务。项目源码按每个任务的计划 SHA 恢复到 `/data/runs`，完整源码、日志和产物在云主机持久卷，关键 checkpoint/snapshot/验收进入既有控制分支。

## 停止、备份与恢复

```bash
bash deploy/cloudkit.sh stop
bash deploy/cloudkit.sh backup /secure-backups/cloudkit-20261027.tar.gz
```

备份先停 worker/dispatcher/Redis，归档 `control` 和 `runs`，写 SHA-256 旁文件。服务保持停止，`up` 重新运行真实就绪检查后恢复。备份可能包含私有源码和任务输出，应存入受控、加密云盘。不会复制 Codex 登录缓存或 GitHub secret，不会迁移到 USB。Redis 不是真相源，不打包 Redis 队列；恢复后从 Git outbox 重建。

恢复只允许空的 control/runs 目标；同名已有数据完整保留并报错。优先在另一台云主机或不同 `COMPOSE_PROJECT_NAME` 创建干净卷验证恢复：

```bash
bash deploy/cloudkit.sh build
bash deploy/cloudkit.sh restore /secure-backups/cloudkit-20261027.tar.gz
bash deploy/cloudkit.sh login
bash deploy/cloudkit.sh up
```

需要与归档同路径的 `.sha256` 旁文件。恢复按旁文件里的 digest 校验本次实际输入归档，再解包；可以把二者一起移动到另一台主机或目录。归档解包限制根目录和链接目标，不允许覆盖 home/auth。备份是本地工作副本/产物备份；远端控制分支仍需保留访问权限及 Git 备份策略。

`stop` 不删除任何卷。本包不提供自动销毁生产卷的命令。主机启动自动恢复取决于 Docker 服务和容器重启策略；24 小时后新启动若探针已过期，需 `up` 重新验证，不会冒充持续在线。

## 自动验证与已知范围

```bash
PYTHONPATH=src python -m unittest discover -s deploy -p 'test_*.py' -v
bash -n deploy/*.sh
bash deploy/smoke.sh
```

离线测试覆盖到期重投、未来任务/终态/有效租约禁止投递，以及配置冲突保留认证。`smoke.sh` 在独立临时 Compose 项目构建镜像、验证非 root 持久目录和 Redis、确认无真实探针时拒绝就绪、验证备份恢复，退出时仅清理本次测试卷。它不用真实 GitHub token、不会调用模型，也不代表生产模型闭环验收。

要证明生产闭环，还必须在目标云主机执行 `up`，提交一个明确验收的真实任务，确认 `DONE` 和验收报告，再重启并检查无重复执行。要证明容灾，另行做云主机断开/恢复和模型临时故障演练。GitHub Actions 的 `autonomy.yml` 后备路径需按原文档单独部署真正的 self-hosted runner；这份 Compose 没有虚构或冒充 GitHub runner。

## 固定输入与官方依据

- Python 运行时 `python:3.12.14-slim-bookworm`：[Docker 官方镜像清单](https://github.com/docker-library/official-images/blob/master/library/python)。保持与 queue 依赖锁解析环境的 Python 3.12 一致；不使用 Debian 默认 Python 3.11，避免触发未列入锁文件的条件依赖。Node/Codex 从同发行版官方构建阶段复制，保留版本与 hash 验证。
- Node 官方镜像 `node:22.23.4-bookworm-slim`：[Docker Hub](https://hub.docker.com/layers/library/node/22.23.4-bookworm-slim/images/sha256-3ba69439667885bb087b138d1e0d7403cd6c3081089b67fdee46d3c76bfd67ac)、[Node 22.23.4 官方安全发布](https://nodejs.org/pt-br/blog/release/v22.23.4)。使用完整版本 tag；尚未在此环境拉取 manifest digest，不伪造 digest 锁定。
- Codex npm `@openai/codex@0.162.1`：[OpenAI 官方发布](https://github.com/openai/codex/releases/tag/rust-v0.162.1)。构建输出实际 `codex --version`，升级通过 `CODEX_VERSION` 明确变更并跑 smoke。
- Redis `redis:7.2.16-alpine`：[官方镜像清单](https://github.com/docker-library/official-images/blob/master/library/redis)。RQ/Python依赖使用原项目 `src/cloudkit/assets/queue/requirements.lock` 的完整版本和 hashes。
- [Docker Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)、[文件型 secrets](https://docs.docker.com/compose/how-tos/use-secrets/)。Debian系统依赖由官方仓库安装，并非完全可重现的 snapshot 构建。
- [Codex 官方认证说明](https://developers.openai.com/codex/auth/)：无头设备登录、凭据缓存与文件存储。

后勤负责人按固定变更周期更新版本、查许可证/安全公告、构建新 tag、通过 smoke 与真实小任务验收后升级。免费额度和成本属于资源台账；这套部署不注册虚假身份、不绕过验证码、不自动填写银行卡。
