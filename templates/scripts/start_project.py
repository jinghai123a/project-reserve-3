#!/usr/bin/env python3
"""Create project intake artifacts; delegate delivery and execution to existing tools."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class InputError(ValueError):
    pass


def require(value, message):
    if not value:
        raise InputError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def text_value(value, label):
    require(isinstance(value, str) and value.strip() and len(value) <= 10000,
            label + " must be nonempty text, at most 10000 characters")
    require(not any(ord(char) < 32 and char not in "\n\t" for char in value),
            label + " contains control characters")
    require(not re.search(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN .*PRIVATE KEY-----)", value),
            label + " must not contain credential-shaped values")
    return value.strip()


def checked_path(value, *, exists):
    path = Path(value).expanduser()
    require(path.is_absolute() and ".." not in path.parts,
            "Path must be absolute and must not contain '..'")
    require(not any(item.is_symlink() for item in (path, *path.parents)),
            "Symlinks are not allowed in project paths")
    if exists:
        require(path.is_dir(), "Project directory does not exist")
    else:
        require(path.parent.is_dir(), "Output parent must already exist")
        require(not path.exists(), "Output already exists; existing projects are never overwritten")
    return path


def validate_spec(spec, *, allow_example=False):
    require(isinstance(spec, dict) and spec.get("schema_version") == 1, "Unsupported intake schema")
    allowed = {"schema_version", "example_only", "project_id", "repository", "control_repository", "branch",
               "goal", "acceptance", "profile", "data_classification", "environment_name", "budget",
               "primary_solution", "fallback_solution"}
    require(set(spec) <= allowed, "Unsupported intake fields; only documented fields are accepted")
    require(isinstance(spec.get("example_only", False), bool), "example_only must be boolean")
    require(allow_example or spec.get("example_only") is not True,
            "Synthetic intake requires --allow-example and is never a real registration")
    identifier = spec.get("project_id")
    require(isinstance(identifier, str) and len(identifier) <= 50 and SLUG.fullmatch(identifier),
            "project_id must be a lowercase hyphenated slug of at most 50 characters")
    repository = spec.get("repository")
    require(isinstance(repository, str) and REPO.fullmatch(repository) and not repository.endswith(".git"),
            "repository must be owner/repository without credentials, URL or .git")
    reserved = read_json(ROOT / "config/company-repositories.json")["repositories"].values()
    require(repository.casefold() not in {item.casefold() for item in reserved},
            "Company repositories cannot be registered as customer projects")
    if "control_repository" in spec:
        require(spec["control_repository"] == repository, "control_repository must equal repository")
    branch = spec.get("branch")
    require(isinstance(branch, str) and len(branch) <= 200 and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) and
            branch != "HEAD" and ".." not in branch and not branch.endswith(("/", ".")) and
            all(item and not item.startswith(".") and not item.endswith(".lock") for item in branch.split("/")),
            "Unsafe or missing branch")
    text_value(spec.get("goal"), "goal")
    criteria = spec.get("acceptance")
    require(isinstance(criteria, list) and 1 <= len(criteria) <= 100, "acceptance requires 1–100 business criteria")
    for criterion in criteria:
        text_value(criterion, "acceptance criterion")
    require(len(set(criteria)) == len(criteria), "Duplicate acceptance criteria")
    profiles = read_json(ROOT / "config/editor-profiles.json")["profiles"]
    require(spec.get("profile") in profiles, "Unknown editor/tool profile")
    require(spec.get("data_classification") in ("public", "internal", "confidential", "restricted"),
            "Data classification is required")
    text_value(spec.get("environment_name"), "environment_name")
    budget = spec.get("budget")
    require(isinstance(budget, dict) and set(budget) == {"currency", "maximum"},
            "budget accepts only currency and maximum")
    require(isinstance(budget, dict) and re.fullmatch(r"[A-Z]{3}", str(budget.get("currency", ""))),
            "budget.currency must be a three-letter currency")
    maximum = budget.get("maximum")
    require(isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and
            maximum >= 0 and maximum < float("inf"), "budget.maximum must be a finite nonnegative number")
    for key, fields in (("primary_solution", ("summary", "verification")),
                        ("fallback_solution", ("summary", "switch_condition", "coverage_gap"))):
        require(isinstance(spec.get(key), dict), key + " is required")
        require(set(spec[key]) == set(fields), key + " accepts only documented fields")
        for field in fields:
            text_value(spec[key].get(field), key + "." + field)
    return spec


def artifact_files(spec):
    profile = read_json(ROOT / "config/editor-profiles.json")
    criteria = [{"id": "acceptance-" + str(index), "description": criterion,
                 "status": "not_run", "evidence_ref": None}
                for index, criterion in enumerate(spec["acceptance"], 1)]
    project = {"schema_version": 1, "id": spec["project_id"], "repository": spec["repository"],
               "branch": spec["branch"], "state_repository": spec["repository"],
               "state_branch": "automation/state", "goal": spec["goal"],
               "source_sha": None, "configuration_status": "DRAFT_NOT_REGISTERED",
               "example_only": spec.get("example_only", False),
               "environment_name": spec["environment_name"], "data_classification": spec["data_classification"],
               "profile": spec["profile"], "budget": spec["budget"],
               "owners": {"delivery": "delivery_commander", "customer_contact": "product_manager",
                          "independent_review": "plan_reviewer", "operations": "operations_engineer"},
               "production_ready": False, "customer_accepted": False}
    plan = {"schema_version": 1, "primary": spec["primary_solution"], "fallback": spec["fallback_solution"],
            "review_status": "pending_independent_review", "reviewer_actor": None,
            "implementation_author_actor": None, "input_revision": None}
    delivery = {"schema_version": 1, "project_id": spec["project_id"], "status": "not_started",
                "required_evidence": ["business_acceptance", "independent_qa", "human_operation_demo",
                                      "versioned_artifacts", "ownership_and_licenses", "operating_instructions",
                                      "deployment_and_health_if_applicable", "restore_and_rollback_if_applicable",
                                      "cost_and_support_terms", "customer_receipt"],
                "evidence_refs": [], "customer_receipt": None, "completed": False}
    tools = {"schema_version": 1, "project_id": spec["project_id"], "profile": spec["profile"],
             "environment_repository": read_json(ROOT / "config/company-repositories.json").get("distribution_repository"), "probe_receipt": None,
             "primary_tools": [], "backup_tools": [], "backup_verified": False,
             "dependency_lockfiles": [], "installed": False, "usable": False,
             "environment_published": False, "model_authenticated": False,
             "credentials_policy": "references_only_use_authorized_secret_store"}
    rules = """# 项目工作规则

先读 .company/project.json、acceptance.json 与 solution.json，确认真实目标、当前版本、输入和验收。当前材料为起步配置，未登记、未运行、未签收。若 example_only=true，只允许合成演练，不能登记真实客户。

项目源码、依赖锁文件、验收与 automation/state 属于本业务仓库。部署复用公开发行包的 deploy 配置，角色随发行包提供。先读取当前项目已有规则，按方案启用实际需要的岗位、工具和检查。

每个实际参与部门和小组完成阶段或暂停前，在本项目 .company/stages 保存总结、评分依据、未完成项和下一步；原始证据保留本项目。接手先核对项目仓库、源码版本、阶段及输入指纹。通用流程与评分样例见发行包 docs/WORKFLOW.zh-CN.md。

一个写入范围同一时段只有一位负责人，方案作者与批准者独立。准备一条主线和一条有切换条件的备用路线，先跑最小用户流程。任务交接带输入版本、产物、验收、下一步和未验证项。不能把工具安装、配置检查、测试通过、上线和客户签收混为一谈。

客户需要操作与业务结果，交付包含真实演示、完整产物、使用说明、费用与支持、适用的回退/恢复。凭据不进 Git、提示词、日志和记忆。缺凭据只阻塞依赖该能力的任务，继续其他工作；不反复原样重试，不清理未知归属资源。
"""
    return {
        ".company/project.json": json_text(project), ".company/acceptance.json": json_text({"schema_version": 1, "criteria": criteria}),
        ".company/solution.json": json_text(plan), ".company/delivery.json": json_text(delivery),
        ".company/tool-readiness.json": json_text(tools),
        ".vscode/extensions.json": json_text({"recommendations": profile["common"] + profile["profiles"][spec["profile"]]}),
        ".vscode/settings.json": json_text({"files.insertFinalNewline": True, "files.trimTrailingWhitespace": True,
                                            "editor.formatOnSave": False, "editor.detectIndentation": True}),
        ".editorconfig": "root = true\n\n[*]\ncharset = utf-8\nend_of_line = lf\ninsert_final_newline = true\ntrim_trailing_whitespace = true\n",
        ".gitignore": ".env\n.env.*\n!.env.example\n.venv/\nnode_modules/\n__pycache__/\n*.py[cod]\n.company/secrets/\n.company/local-evidence/\n",
        "AGENTS.md": rules,
        "README.md": "# " + spec["project_id"] + "\n\n" + spec["goal"] + "\n\n此目录目前只有项目起步材料，没有应用程序或已安装的业务依赖。按 `.company/acceptance.json` 实现并验证真实用户流程。\n\n"
                     + "公司身份与控制仓库在 `.company/project.json`，主备方案在 `.company/solution.json`，工具与交割状态分别在对应 JSON 中。客户业务依赖必须在本仓库锁定。\n"
    }


def initialize(spec, output, *, allow_example=False):
    validate_spec(spec, allow_example=allow_example)
    destination = checked_path(output, exists=False)
    files = artifact_files(spec)
    # Exclusive creation is the ownership boundary; never remove a partially written project.
    destination.mkdir()
    hashes = {}
    for relative, body in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as handle:
            handle.write(body)
        hashes[relative] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    receipt = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
               "kind": "project_template_generation", "project_id": spec["project_id"],
               "example_only": spec.get("example_only", False), "generated_files_sha256": hashes,
               "template_inputs_sha256": {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                                           for relative in ("scripts/start_project.py", "config/company-repositories.json",
                                                            "config/editor-profiles.json")},
               "intake_sha256": hashlib.sha256(json_text(spec).encode("utf-8")).hexdigest(),
               "registered": False, "application_created": False, "dependencies_installed": False,
               "environment_published": False, "production_ready": False}
    with (destination / ".company/generation-receipt.json").open("x", encoding="utf-8") as handle:
        handle.write(json_text(receipt))
    return receipt


def git(root, *args, raw=False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not raw, timeout=30)
    require(result.returncode == 0, "Git check failed: " + " ".join(args[:2]))
    return result.stdout if raw else result.stdout.strip()


def registration(project_root, hq_root=None):
    root = checked_path(project_root, exists=True)
    hq = checked_path(hq_root, exists=True) if hq_root is not None else None
    if hq is not None:
        require((hq / "scripts/office.py").is_file(), "HQ registration entrypoint does not exist")
    require(Path(git(root, "rev-parse", "--show-toplevel")).resolve() == root.resolve(),
            "Project must be its own Git worktree, not a folder inside another repository")
    require(not git(root, "status", "--porcelain"), "Commit project files before preparing registration")
    sha = git(root, "rev-parse", "HEAD")
    require(re.fullmatch(r"[0-9a-f]{40}", sha), "A full 40-character source SHA is required by HQ")
    committed = {}
    for relative in (".company/project.json", ".company/acceptance.json"):
        path = root / relative
        require(path.is_file() and not path.is_symlink() and not path.parent.is_symlink(),
                "Registration input must be a regular project-owned file")
        git(root, "ls-files", "--error-unmatch", relative)
        committed_bytes = git(root, "show", sha + ":" + relative, raw=True)
        require(path.read_bytes() == committed_bytes,
                "Registration input differs from the committed Git object; commit the actual material first")
        committed[relative] = json.loads(committed_bytes)
    project = committed[".company/project.json"]
    require(isinstance(project, dict) and project.get("schema_version") == 1, "Invalid project schema")
    require(project.get("example_only") is False, "Synthetic projects cannot be registered")
    repository = project.get("repository", "")
    require(REPO.fullmatch(repository) and not repository.endswith(".git"), "Invalid repository identity")
    require(project.get("state_repository") == repository and project.get("state_branch") == "automation/state",
            "Project state must belong to its own repository and automation/state branch")
    reserved = read_json(ROOT / "config/company-repositories.json")["repositories"].values()
    require(repository.casefold() not in {item.casefold() for item in reserved}, "Company repository is not a customer project")
    origin = git(root, "remote", "get-url", "origin")
    permitted = {"https://github.com/" + repository, "https://github.com/" + repository + ".git",
                 "git@github.com:" + repository, "git@github.com:" + repository + ".git"}
    require(origin in permitted, "Git origin does not match the declared repository")
    require(git(root, "branch", "--show-current") == project.get("branch"), "Current branch does not match project branch")
    require(git(root, "rev-parse", "HEAD") == sha, "Project HEAD changed while checking registration")
    criteria = committed[".company/acceptance.json"].get("criteria")
    require(isinstance(criteria, list) and bool(criteria), "Acceptance criteria are missing")
    require(isinstance(project.get("id"), str) and len(project["id"]) <= 50 and SLUG.fullmatch(project["id"]),
            "Invalid project id")
    if hq is None:
        for criterion in criteria:
            require(isinstance(criterion, dict), "Acceptance criterion must be an object")
            text_value(criterion.get("description"), "acceptance criterion")
        return {"kind": "project_identity", "project_id": project["id"], "source_sha": sha,
                "repository": repository, "branch": project["branch"],
                "acceptance": criteria, "registered": False, "remote_sha_verified": False,
                "next_action": "Use this verified identity in the task plan and deploy/.env; follow docs/company.zh-CN.md."}
    argv = ["python3", str(hq / "scripts/office.py"), "--root", str(hq), "register", "--id", project["id"],
            "--repository", repository, "--goal", text_value(project["goal"], "goal"),
            "--source-sha", sha, "--branch", project["branch"]]
    for criterion in criteria:
        require(isinstance(criterion, dict), "Acceptance criterion must be an object")
        argv += ["--acceptance", text_value(criterion.get("description"), "acceptance criterion")]
    return {"kind": "registration_proposal", "source_sha": sha, "repository": repository,
            "argv": argv, "registered": False, "remote_sha_verified": False,
            "next_action": "Verify this commit exists on the authorized remote, then invoke HQ with this argument array. This command makes no registration or remote mutation."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("--spec", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--allow-example", action="store_true")
    register = commands.add_parser("registration")
    register.add_argument("--project-root", required=True)
    register.add_argument("--hq-root", help="Optional existing headquarters integration")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(read_json(args.spec), args.output, allow_example=args.allow_example)
        else:
            result = registration(args.project_root, args.hq_root)
        print(json_text(result), end="")
        return 0
    except (InputError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(json_text({"ok": False, "error": str(error), "production_ready": False}), file=sys.stderr, end="")
        return 1


if __name__ == "__main__":
    sys.exit(main())
