# 云端运行所需配置

## 开源仓库维护

| 项目 | 配置位置 | 操作 |
| --- | --- | --- |
| 仓库名称 | GitHub → Settings → General → Repository name | 建议使用 `cloud-company-kit`；当前公开仓地址以所在仓库为准 |
| About 描述 | 仓库首页 About | `Plan-driven multi-agent cloud execution with durable state, independent verification and recovery.` |
| Topics | 仓库首页 About | `ai-agents`, `multi-agent`, `codex`, `workflow`, `docker`, `self-hosted`, `automation`, `developer-tools` |
| Actions | Settings → Actions → General | 开启本仓库工作流；检查首次运行结果 |
| Social preview | Settings → General → Social preview | 可上传项目演示截图 |

名称、主题和清楚的运行示例有助于理解与发现项目。搜索与 Trending 排序由 GitHub 决定。

## 实际项目部署

| 必需项 | 配置位置 | 验证 |
| --- | --- | --- |
| Linux 云主机、持久磁盘、Docker/Compose | 自选云服务；不依赖个人电脑 | `docker version`、`docker compose version` |
| 独立业务仓库 | `deploy/.env` 两个仓库身份字段 | `cloudkit.sh up` 验证 Git 读写与项目身份 |
| Git 凭据 | `deploy/secrets/github_token` 或指定秘密文件 | 容器用户能读取，仓库访问成功 |
| Codex 账号认证 | `bash deploy/cloudkit.sh login` | 账号所有者完成一次设备验证；`ready` 进行真实模型探针 |
| 每项目独立卷名称 | `COMPOSE_PROJECT_NAME` | 项目身份与持久目录匹配 |
| 随包角色配置 | `.env` 中 `CLOUDKIT_AGENT_CONFIG_SOURCE` | `up` 导入并检查已有配置差异 |

真实模型执行使用账号实际额度，云主机按所选服务计费。仓库代码和离线演练本身不需要填写付款资料。

## 按方案需要再准备

- 联网研究：可控制的 Firefox、Chrome、Edge，以及目标资料网络访问。
- 语义记忆：memory profile、模型文件和独立持久目录。
- 语言/设计工具：项目对应 profile 和项目锁文件。
- 私网运维：现有 `deploy/network/` 配置及实际 VPN 身份。
- 可选情报 API：对应服务的账号凭据与访问额度。

阶段结束把实际缺口整理为一张配置表，继续执行不依赖这些缺口的任务。
