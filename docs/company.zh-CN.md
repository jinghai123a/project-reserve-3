# 多部门协作：单一状态、明确交接、独立验收

组织目录与工作规约在 [随包组织清单](../company/organization.json)与[公共工作流](WORKFLOW.zh-CN.md)。本仓库负责执行：新增的 `cloudkit.runtime.company` 是一次性任务图协调器，直接复用已有 Git 控制状态、worker supervisor、Redis/RQ 和 dispatcher；没有额外常驻编排服务或数据库。

## 实际执行路径

方案规划者提交带版本的任务图，独立审核者检查需求、接口、依赖与验收。协调器只派发已审核且依赖通过的任务；专业执行者产出文件，supervisor 实际执行验收命令，协调器再核实产物哈希和写入范围。后续任务收到上游产物引用与哈希，不必重复阅读整个聊天。最终指挥核对用户目标与验收证据，运维负责目标环境健康与发布回执。

任务状态和审核记录进入 `STATE.json` 的 `coordination.goals`；交接文件在控制分支的 `coordination/<goal_id>/handoffs/`。注册前持久化派发预留，重启按同一 worker ID 补完注册，避免因进程中断重复创建任务。无状态变化的协调轮次不产生空提交。

## 接口与配置

| 命令 | 必需输入 | 结果 |
| --- | --- | --- |
| `python -m cloudkit.runtime.company --repository PATH register --plan FILE` | 已初始化的 Git 控制仓库、完整 JSON 方案 | goal ID 与不可变 plan hash |
| `approve --goal-id ID --reviewer ACTOR --plan-hash HASH --evidence FILE` | 作者之外的审核身份、匹配方案哈希、实际审核文件 | 审核证据和哈希进入控制分支 |
| `reconcile --goal-id ID --project-root PATH` | 实际项目 checkout；首次 HEAD 与 source_sha 一致 | 绑定工作区，派发就绪任务、接纳合格产物 |
| `status --goal-id ID` | 已注册目标 | 当前状态、任务和证据引用 |

后三个子命令均接在相同的 `python -m cloudkit.runtime.company --repository PATH` 之后。表中参数表示已有资源，不是可直接运行的示例；下面的演练命令会自动生成完整输入。

方案字段：`schema_version=1`、`goal_id`、`author`、`project_id`、`branch`、`source_sha`、`goal`、`tasks`，可选 `max_parallel`（1–6）。每个任务包括 `id`、`role`、`owner`、`dependencies`、`write_scope`、完整 supervisor `worker_plan`。验收命令和后端在既有 worker plan 中配置，不创建第二套模型路由。

`write_scope` 是规范化相对文件或目录路径，不是 glob。依赖必须无环；下游不得改写祖先已验收的产物。需要修改已批准输入时，建立新目标版本并重新审核。审核身份目前是受信入口提供的不同 actor 声明，不是密码学身份认证，入口权限仍由部署环境控制。

首次带真实项目路径协调后，既有 `deploy/dispatch.py` 会继续推进已审核目标，队列消费者使用绑定的工作区。依赖型任务没有共享路径时等待绑定；无法读取输出或缺少执行前基线时不接纳写任务完成声明。现有 [部署说明](../deploy/README.zh-CN.md) 负责持久容器、凭据及健康检查。

## 并行与恢复约束

- 同一共享工作区一次只有一个顶层 worker 写入；worker 内可按组织规约委派不冲突的子任务。独立项目可并行，角色目录不等于全部岗位同时启动。
- 任务派发前保存可见源码基线；非空写入范围必须有真实输出。验收时检测范围外新增、改动和删除；这是事后拒收，不是操作系统写入沙箱。越界现场保留作证。
- 依赖产物哈希绑定到工作计划。worker 持工作区锁、恢复完成后再验证输入；输入变动阻塞当前任务，不调用模型消费过期上下文。
- 等待能力且无唤醒时间的任务不会堵住独立任务。重复错误沿用 supervisor 的有限重试和证据归档，不删除整个工程或无条件更换技术栈。
- Git 控制状态支持跨进程恢复，工作区数据必须另有持久卷和备份。入口授权、模型凭据、真实云资源必须存在后才能称为持续在线；配置和 CI 不能证明这一点。

## 可复现完整演练

从仓库根目录运行：

```bash
python -m pip install .
python examples/company_rehearsal.py --output verification/company-rehearsal.json
python examples/company_demo/verify.py --output verification/company-http.json
```

首个命令后的演练建立真实临时 Git 控制库和项目，执行“规格→实现→验证”的子进程任务图，检查独立审核、依赖、过期输入、状态恢复及空轮次无提交。它明确使用 `command_fixture`，不声称调用了模型。HTTP 演示则实际启动 [TaskDesk](../examples/company_demo/README.md)，验证接口、错误和 SQLite 重启持久性。浏览器 CI 另外运行真实页面交互并保存截图。

`.github/workflows/company-demo.yml` 保存 JSON 与截图证据；原有核心、原生记忆、队列、Docker smoke CI 继续运行。通过这些检查表示所覆盖路径成立，不承诺所有故障均能秒级恢复。快速恢复依靠已有健康检查、明确错误分类、保留检查点和经过验证的重试/回退路径。
