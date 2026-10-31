# 云端升级说明

基于上游 a5c3b34373a5d87a438421eb106954da3da1ab7b。保留现有共享工具、独立业务仓库、Git 持久状态和独立验收结构。

## 已实现的恢复改动

- 临时服务、网络和限流探测失败进入持久化冷却；到期重新验证，避免必须更改程序文件才能恢复。鉴权、配置和协议错误仍保留明确故障状态。
- 渐进修复区分文件变化与独立验收通过项。允许第三轮修复成功，同时限制没有验收进展的持续代码扰动。默认 `max_unverified_rounds=3`，可设为 3–50。
- 命令任务每轮使用独立恢复目录，验证 Git 项目身份后恢复检查点；保留旧目录和已有产物，支持同机续跑和运行器丢失后的恢复。

后端可设置 `probe_retry_seconds`，默认 60 秒，指数退避最高 3600 秒。冷却保存 `next_wakeup_at`，须由部署调度器在到期后唤醒；调用一次 CLI 不等于已建立后台持续执行。

## 部署与组织

入口见 [部署说明](../deploy/README.zh-CN.md)；真实云服务器需要 Docker Engine、Compose、Git 仓库访问和实际模型认证。部署探测通过后才启动消费者。

部门角色、个性化提示词及选型研究维护于 [随包角色配置](../.codex/config.toml)和[工作流](WORKFLOW.zh-CN.md)。角色配置按需实例化，不为每个职位启动常驻进程。DOT/Work 是产品接入点，不作为容器镜像中的虚构模型后端。

[可选私有管理网络](../deploy/network/README.zh-CN.md) 提供 Tailscale 配置，与任务执行服务分开；网络认证缺失只阻塞私有网络接入。[外部情报工具](intelligence.zh-CN.md) 提供 VirusTotal、Shodan、Maps 的独立调用入口，不依赖 AWS。

## 验证与限制

`PYTHONPATH=src python -m unittest discover -s tests -v` 运行 Python 回归测试；GitHub Actions 运行 Python 版本矩阵、原生记忆、队列和 Docker 部署冒烟。CI 中无模型凭证的测试不证明真实模型可用。

本次没有改变整个记忆检索层，也没有宣称解决模型自身全部表现问题。每轮模型执行仍须读取项目 AGENTS.md 与持久检查点；验收命令应来自可信项目规则，外部写入需要幂等或动作核对。
