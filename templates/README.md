# 项目起步模板

把需求整理为独立项目的验收、主备方案、工具准备、交割与编辑器配置。生成器使用 Python 标准库。

## 运行合成示例

从发行包根目录执行：

```bash
python3 -m unittest discover -s templates/tests -v
python3 - <<'PYDEMO'
from pathlib import Path
import tempfile, subprocess
parent = Path(tempfile.mkdtemp(prefix="project-template-"))
subprocess.run(["python3", "templates/scripts/start_project.py", "init",
    "--spec", "templates/examples/synthetic-intake.json", "--output", str(parent / "project"),
    "--allow-example"], check=True)
print(parent / "project")
PYDEMO
```

真实项目使用自己的接单 JSON：项目标识、仓库、分支、目标、验收、数据分类、环境名、预算、推荐方案及备用方案。示例字段见 examples/synthetic-intake.json。输出路径为尚未存在的新目录，已有项目可先生成旁路材料再合并。

## 对接运行时

在独立业务仓库提交源码和起步材料后，使用以下命令读取并核验 Git 身份：

```bash
python3 templates/scripts/start_project.py registration --project-root /absolute/path/to/business-project
```

命令检查真实 HEAD、仓库身份、分支和已提交输入，输出可用于任务计划的项目身份。随后按发行包 docs/company.zh-CN.md 登记任务图，按 deploy/README.zh-CN.md 启动执行环境。已有独立总部工具的安装仍可选传 --hq-root，基础使用不依赖它。

config/company-repositories.json 可填写部署者自己的基础设施仓库清单及 distribution_repository；默认只有虚构示例。编辑器 profile 仅推荐对应扩展，实际语言依赖按项目锁文件安装。

阶段记录使用 ../company/stage-report.template.json，评分与按方案触发规则见 ../docs/WORKFLOW.zh-CN.md。生成回执包含输入和输出哈希，后续功能验证以实际项目版本为准。
