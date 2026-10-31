# 可选云主机私有管理网络

这是独立于任务 worker 的 Tailscale 用户态网络，使用官方镜像 `ghcr.io/tailscale/tailscale:v1.104.2`。它把同一 tailnet 内的 `cloudkit-admin:2222` 转发到云主机现有 SSH 服务 `host.docker.internal:22`。SSH 仍使用主机原有账号/公钥，明确关闭 Tailscale SSH 登录功能（`--ssh=false`）。未安装或未监听 Docker 宿主网关的 SSH 服务，需要先按云主机标准方式配置；本包不创建密码、不修改 sshd。

配置不需要 `NET_ADMIN` 或 `/dev/net/tun`，不发布公网端口、不开放 Redis、不启用 Funnel、不广告或接收子网/出口路由，也不修改工作容器 DNS/API 出站路径。网络服务停止、未认证或不可达均不阻塞默认 CloudKit 任务系统。它面向你获授权的云主机私有管理；不会改变当前对话执行环境的网络限制。

## 首次启用

1. 在自己的 Tailscale 组织中把 `policy.example.json` 的 tagOwners/grants **合并进已有策略**并审核。只允许 tailnet 管理员访问此节点 TCP 2222；已有宽泛 allow-all 规则会继续生效，须由网络负责人按现有组织需求收窄。本包不会远程覆盖你的组织策略。
2. 准备组织授权的 auth key，带 `tag:cloudkit-admin`，用于这台节点；推荐预授权、非临时节点 key。已有持久登录可复用，不需要反复注册账号。
3. 在目标云主机创建私密目录，从该主机密钥管理器写入 key 文件，不要把 key 放进 Git、`.env`、聊天或命令行：

```bash
install -d -m 700 deploy/network/secrets
install -m 600 /dev/null deploy/network/secrets/tailscale_authkey
```

写入真实 key 后启动：

```bash
bash deploy/network/network.sh up
bash deploy/network/network.sh ready
```

也可用环境变量 `CLOUDKIT_TAILSCALE_AUTHKEY_FILE` 指向云主机现有密钥文件的**绝对路径**。默认相对路径以 `deploy/network/` 为基准。`CLOUDKIT_TAILSCALE_HOSTNAME` 可覆盖节点名，`CLOUDKIT_NETWORK_PROJECT` 可区分不同云主机。

入口脚本仅把文件内容读入容器进程内存；Compose 元数据没有 key 值。认证状态持久化到独立命名卷，`TS_AUTH_ONCE=true` 避免正常重启重复认证。文件缺失会阻止此可选服务创建；文件为空且无已有登录时保持未连接，`up --wait` 返回失败，不会假报成功。不要删除状态卷来处理普通重启。

## 从授权设备连接

管理设备先使用同一组织的正式 Tailscale 客户端登录。用 `network.sh up` 返回的 tailnet IPv4，或组织已启用 MagicDNS 时的 `cloudkit-admin` 主机名：

```bash
ssh -p 2222 your-existing-cloud-user@cloudkit-admin
```

`your-existing-cloud-user` 是你现有云主机用户名。SSH 主机指纹应与该云主机现有指纹一致。若无法连接，检查 tailnet grants、节点在线状态、宿主机 sshd 监听和主机防火墙是否允许该 Docker bridge 到 TCP 22；无需打开公网 TCP 2222。现有公网 SSH 是否关闭属于云主机访问策略，确认私有连接可用后再单独变更。

```bash
bash deploy/network/network.sh status
bash deploy/network/network.sh logs
bash deploy/network/network.sh stop
```

健康检查只验证已获得 tailnet 地址，`ready` 另显示 Serve 配置；**实际跨设备 SSH 成功**才是管理通路验收。不会将网络健康当成模型任务就绪。网络账号/授权未提供时，交付状态只能是“配置已准备，尚未连接”。

## 验证与版本

```bash
bash deploy/network/validate.sh
```

该检查只渲染 Compose 并校验 JSON：独立服务、官方固定镜像、用户态、无新增权限、公网端口/出口路由缺失、密钥文件注入、唯一 SSH 目标和最小策略。不会登录、创建账户、启动 VPN 或接触付款信息。Docker 镜像拉取及真实跨设备连通仍需在获授权云主机验证。

官方依据：

- [官方容器 v1.104.2 标签](https://github.com/tailscale/tailscale/pkgs/container/tailscale/versions?filters%5Bversion_type%5D=tagged)；[对应 release](https://github.com/tailscale/tailscale/releases/tag/v1.104.2)。
- [Docker 参数](https://tailscale.com/docs/features/containers/docker/docker-params)：`TS_USERSPACE`、持久状态、一次认证、Serve目录挂载及健康端点。
- [官方 Serve 数据结构](https://github.com/tailscale/tailscale/blob/main/ipn/serve.go)：TCPForward 支持 host:port，SSH 经原协议端到端认证。
- [官方容器启动实现](https://github.com/tailscale/tailscale/blob/main/Dockerfile)：`/usr/local/bin/containerboot`。
