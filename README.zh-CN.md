# Cloud Company Kit

**按方案调用 Agent，在云端持续执行，用真实证据完成交付。**

一套可自行部署的 AI 软件交付工作台：一个负责人按方案分派任务，专业角色按需参与，Git 保存任务状态与检查点，独立验收确认结果。

[English](README.md) · [工作流](docs/WORKFLOW.zh-CN.md) · [云端部署](deploy/README.zh-CN.md) · [所需配置](docs/CONFIGURE.zh-CN.md) · [可操作演示](examples/company_demo/README.md)

## 解决什么问题

- 长任务中断后，能根据版本、输入和检查点接续。
- 只启动当前阶段需要的岗位、工具与测试。
- 某任务缺少能力时，继续推进其他已就绪任务。
- 复用服务器配置、容器和成熟工具，减少重复开发。
- 项目代码、状态和业务数据各归自己的仓库与持久卷。
- 阶段结束留下结果、证据、评分依据、未完成项和下一步。

28 个角色覆盖 14 类职责，随工单调用。核心仍是一套 Git 状态机、监督器和 Redis/RQ 唤醒链；共享工作区同一时间由一个顶层执行者写入。

## 先跑完整演练

需要 Python 3.11+ 和 Git。以下演练无需云账号或模型额度：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
cloudkit setup --runtime-root /tmp/cloud-company-kit-runtime
cloudkit doctor --runtime-root /tmp/cloud-company-kit-runtime
python examples/company_rehearsal.py --output verification/company-rehearsal.json
python examples/company_demo/verify.py --output verification/company-http.json
```

演练会创建临时 Git 仓库，执行规格、实现、验证与恢复流程。TaskDesk 演示实际 HTTP 和 SQLite 持久化；演练使用确定性工作进程，真实模型任务使用你自己的 Codex 认证。

## 部署到云端

在持久 Linux 云主机安装 Docker 与 Compose，准备独立业务仓库，复制 `deploy/.env.example` 为 `deploy/.env`，填写仓库身份并通过秘密文件提供 Git 凭据。随后执行：

```bash
bash deploy/cloudkit.sh login
bash deploy/cloudkit.sh up
bash deploy/cloudkit.sh status
```

角色配置随镜像提供，部署不依赖任何私人公司仓库。首次模型设备认证由账号所有者完成一次。完整步骤、健康检查、备份恢复见 [部署说明](deploy/README.zh-CN.md)。

## 工作方式

先定目标、方案和阶段，再选择本阶段适用规则。联网研究才调用 Firefox、Chrome、Edge 和 Wikipedia 资料入口；界面任务才启用设计与交互验证；部署任务才准备目标环境。仅对输入或实现变化的部分重新核验。

每个阶段在业务项目保存报告，按目标35、正确性25、证据20、效率10、交接10记录评分。分数由有证据的评价产生，空白模板不会自动生成成绩。改进以历史失败案例和回归结果验证，再更新工具或工作步骤。

## 项目入口

| 内容 | 位置 |
| --- | --- |
| 运行时与执行链 | `src/cloudkit/` |
| 云端部署、健康与恢复 | `deploy/` |
| 按需角色配置 | `.codex/` |
| 组织清单、阶段报告 | `company/` |
| 接单与主备方案模板 | `templates/` |
| 演示与验证 | `examples/`、`tests/` |

[配置清单](docs/CONFIGURE.zh-CN.md) · [整体架构](docs/ARCHITECTURE_ZH.md) · [协作接口](docs/company.zh-CN.md) · [参与改进](CONTRIBUTING.md)

项目采用 [Unlicense](LICENSE)。选装组件保留各自许可证。
