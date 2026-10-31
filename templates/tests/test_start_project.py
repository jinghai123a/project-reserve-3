import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("start_project", ROOT / "scripts/start_project.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ProjectTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="company-template-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "customer-project"
        self.spec = MODULE.read_json(ROOT / "examples/synthetic-intake.json")
        self.spec["example_only"] = False
        self.hq = self.root / "hq"
        (self.hq / "scripts").mkdir(parents=True)
        (self.hq / "scripts/office.py").write_text("# Test entrypoint; never executed.\n")

    def command(self, *args, cwd=None):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)

    def make_git_project(self):
        MODULE.initialize(self.spec, str(self.output))
        self.command("git", "init", "-b", "main", str(self.output))
        self.command("git", "config", "user.name", "Template Fixture", cwd=self.output)
        self.command("git", "config", "user.email", "fixture@example.invalid", cwd=self.output)
        self.command("git", "remote", "add", "origin", "https://github.com/" + self.spec["repository"] + ".git", cwd=self.output)
        self.commit()

    def commit(self):
        self.command("git", "add", ".", cwd=self.output)
        self.command("git", "commit", "-m", "Synthetic template fixture", cwd=self.output)

    def test_generated_outputs_keep_all_real_execution_claims_false(self):
        receipt = MODULE.initialize(self.spec, str(self.output))
        self.assertEqual(len(receipt["generated_files_sha256"]), 11)
        self.assertEqual(len(receipt["template_inputs_sha256"]), 3)
        for key in ("registered", "application_created", "dependencies_installed", "environment_published", "production_ready"):
            self.assertIs(receipt[key], False)
        project = MODULE.read_json(self.output / ".company/project.json")
        self.assertIsNone(project["source_sha"])
        self.assertEqual(project["state_repository"], self.spec["repository"])
        self.assertIs(project["customer_accepted"], False)
        self.assertEqual(MODULE.read_json(self.output / ".company/solution.json")["fallback"], self.spec["fallback_solution"])

    def test_standalone_identity_does_not_require_private_headquarters(self):
        self.make_git_project()
        result = MODULE.registration(str(self.output))
        self.assertEqual(result["kind"], "project_identity")
        self.assertEqual(result["repository"], self.spec["repository"])
        self.assertEqual(result["source_sha"], MODULE.git(self.output, "rev-parse", "HEAD"))
        self.assertFalse(result["registered"])
        self.assertNotIn("argv", result)

    def test_standalone_identity_rejects_uncommitted_project(self):
        self.make_git_project()
        (self.output / ".company/project.json").write_text("{}")
        with self.assertRaisesRegex(MODULE.InputError, "Commit project files"):
            MODULE.registration(str(self.output))

    def test_existing_empty_directory_is_never_overwritten(self):
        self.output.mkdir()
        with self.assertRaisesRegex(MODULE.InputError, "already exists"):
            MODULE.initialize(self.spec, str(self.output))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_data_is_preserved(self):
        self.output.mkdir()
        marker = self.output / "customer.txt"
        marker.write_text("valuable")
        with self.assertRaises(MODULE.InputError):
            MODULE.initialize(self.spec, str(self.output))
        self.assertEqual(marker.read_text(), "valuable")

    def test_repeated_generation_does_not_overwrite(self):
        MODULE.initialize(self.spec, str(self.output))
        original = (self.output / ".company/project.json").read_bytes()
        with self.assertRaises(MODULE.InputError):
            MODULE.initialize(self.spec, str(self.output))
        self.assertEqual((self.output / ".company/project.json").read_bytes(), original)

    def test_missing_required_field_creates_nothing(self):
        for key in ("project_id", "repository", "branch", "goal", "acceptance", "profile", "data_classification",
                    "environment_name", "budget", "primary_solution", "fallback_solution"):
            with self.subTest(key=key):
                spec = copy.deepcopy(self.spec)
                spec.pop(key)
                with self.assertRaises(MODULE.InputError):
                    MODULE.initialize(spec, str(self.output))
                self.assertFalse(self.output.exists())

    def test_invalid_cost_cannot_look_authorized(self):
        for maximum in (True, -1, float("nan"), float("inf"), "100"):
            with self.subTest(maximum=maximum):
                self.spec["budget"]["maximum"] = maximum
                with self.assertRaises(MODULE.InputError):
                    MODULE.initialize(self.spec, str(self.output))

    def test_synthetic_fixture_requires_explicit_allowance(self):
        self.spec["example_only"] = True
        with self.assertRaisesRegex(MODULE.InputError, "Synthetic"):
            MODULE.initialize(self.spec, str(self.output))
        result = MODULE.initialize(self.spec, str(self.output), allow_example=True)
        self.assertIs(result["example_only"], True)

    def test_cross_project_control_identity_is_rejected(self):
        self.spec["control_repository"] = "another/client"
        with self.assertRaisesRegex(MODULE.InputError, "must equal"):
            MODULE.initialize(self.spec, str(self.output))

    def test_all_company_repositories_are_reserved(self):
        for repository in MODULE.read_json(ROOT / "config/company-repositories.json")["repositories"].values():
            with self.subTest(repository=repository):
                self.spec["repository"] = repository
                with self.assertRaisesRegex(MODULE.InputError, "Company repositories"):
                    MODULE.initialize(self.spec, str(self.output))

    def test_released_reserves_validate_without_creating_projects(self):
        for number in (1, 2, 3):
            spec = copy.deepcopy(self.spec)
            spec["repository"] = "example-org/customer-project-" + str(number)
            if "control_repository" in spec:
                spec["control_repository"] = spec["repository"]
            MODULE.validate_spec(spec)
        self.assertFalse(self.output.exists())

    def test_generated_tool_ownership_uses_headquarters(self):
        files = MODULE.artifact_files(self.spec)
        joined = "\n".join(files.values())
        self.assertNotIn("company-environments", joined)
        self.assertNotIn("company-assurance", joined)
        self.assertIn('"environment_repository": null', joined)

    def test_unsafe_identity_branch_and_duplicate_acceptance_are_rejected(self):
        values = [("repository", "../secret"), ("repository", "https://token@github.com/customer/project"),
                  ("project_id", "../other"), ("branch", "main/../private"), ("branch", "-x"),
                  ("acceptance", ["same", "same"])]
        for key, value in values:
            with self.subTest(key=key, value=value):
                spec = copy.deepcopy(self.spec)
                spec[key] = value
                with self.assertRaises(MODULE.InputError):
                    MODULE.initialize(spec, str(self.output))

    def test_path_traversal_relative_path_and_missing_parent_are_rejected(self):
        for output in ("relative-project", str(self.root / "missing" / "project"), str(self.root / ".." / "escape")):
            with self.subTest(output=output):
                with self.assertRaises(MODULE.InputError):
                    MODULE.initialize(self.spec, output)

    def test_symlink_parent_is_rejected(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(MODULE.InputError, "Symlink"):
            MODULE.initialize(self.spec, str(link / "project"))

    def test_credential_shaped_goal_is_rejected(self):
        self.spec["goal"] = "ghp_" + "a" * 32
        with self.assertRaisesRegex(MODULE.InputError, "credential"):
            MODULE.initialize(self.spec, str(self.output))

    def test_unapproved_nested_or_top_level_fields_create_no_files(self):
        cases = [(None, "secret_value"), ("budget", "secret_value"),
                 ("primary_solution", "password"), ("fallback_solution", "unreviewed_metadata")]
        for parent, key in cases:
            with self.subTest(parent=parent, key=key):
                spec = copy.deepcopy(self.spec)
                target = spec if parent is None else spec[parent]
                target[key] = {"nested": "nonsecret-canary-must-not-be-copied"}
                with self.assertRaises(MODULE.InputError):
                    MODULE.initialize(spec, str(self.output))
                self.assertFalse(self.output.exists())

    def test_credential_shaped_allowed_solution_text_creates_no_files(self):
        for parent, field in (("primary_solution", "summary"), ("fallback_solution", "switch_condition")):
            with self.subTest(parent=parent, field=field):
                spec = copy.deepcopy(self.spec)
                spec[parent][field] = "ghp_" + "testcanary" * 4
                with self.assertRaisesRegex(MODULE.InputError, "credential"):
                    MODULE.initialize(spec, str(self.output))
                self.assertFalse(self.output.exists())

    def test_real_git_registration_proposal_has_actual_sha_without_mutation(self):
        self.make_git_project()
        expected_sha = self.command("git", "rev-parse", "HEAD", cwd=self.output).stdout.strip()
        result = MODULE.registration(str(self.output), str(self.hq))
        self.assertEqual(result["source_sha"], expected_sha)
        self.assertIn(expected_sha, result["argv"])
        self.assertIs(result["registered"], False)
        self.assertIs(result["remote_sha_verified"], False)
        self.assertEqual(self.command("git", "status", "--porcelain", cwd=self.output).stdout, "")

    def test_dirty_worktree_cannot_be_bound_to_old_sha(self):
        self.make_git_project()
        (self.output / "README.md").write_text("Changed customer goal")
        with self.assertRaisesRegex(MODULE.InputError, "Commit"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_hidden_worktree_changes_cannot_be_bound_to_old_sha(self):
        self.make_git_project()
        for flag, relative, field in (("--assume-unchanged", ".company/project.json", "goal"),
                                      ("--skip-worktree", ".company/acceptance.json", "criteria")):
            with self.subTest(flag=flag):
                path = self.output / relative
                original = path.read_bytes()
                self.command("git", "update-index", flag, relative, cwd=self.output)
                data = MODULE.read_json(path)
                if field == "goal":
                    data[field] = "Uncommitted changed goal hidden from git status"
                else:
                    data[field][0]["description"] = "Uncommitted changed acceptance"
                path.write_text(MODULE.json_text(data))
                self.assertEqual(self.command("git", "status", "--porcelain", cwd=self.output).stdout, "")
                with self.assertRaisesRegex(MODULE.InputError, "differs from the committed"):
                    MODULE.registration(str(self.output), str(self.hq))
                path.write_bytes(original)
                undo = "--no-assume-unchanged" if flag == "--assume-unchanged" else "--no-skip-worktree"
                self.command("git", "update-index", undo, relative, cwd=self.output)

    def test_wrong_remote_cannot_register_other_customer(self):
        self.make_git_project()
        self.command("git", "remote", "set-url", "origin", "https://github.com/another/customer.git", cwd=self.output)
        with self.assertRaisesRegex(MODULE.InputError, "origin"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_synthetic_project_cannot_be_registered(self):
        self.make_git_project()
        path = self.output / ".company/project.json"
        project = MODULE.read_json(path)
        project["example_only"] = True
        path.write_text(MODULE.json_text(project))
        self.commit()
        with self.assertRaisesRegex(MODULE.InputError, "Synthetic"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_nested_parent_repo_is_not_customer_repo(self):
        self.command("git", "init", "-b", "main", str(self.root))
        MODULE.initialize(self.spec, str(self.output))
        with self.assertRaisesRegex(MODULE.InputError, "own Git worktree"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_wrong_branch_is_rejected(self):
        self.make_git_project()
        self.command("git", "checkout", "-b", "unreviewed", cwd=self.output)
        with self.assertRaisesRegex(MODULE.InputError, "branch"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_untracked_ignored_registration_inputs_are_rejected(self):
        self.make_git_project()
        self.command("git", "rm", "--cached", ".company/project.json", cwd=self.output)
        with (self.output / ".gitignore").open("a") as handle:
            handle.write(".company/project.json\n")
        self.commit()
        with self.assertRaisesRegex(MODULE.InputError, "Git check failed"):
            MODULE.registration(str(self.output), str(self.hq))

    def test_cli_missing_parameters_and_synthetic_gate(self):
        missing = subprocess.run([sys.executable, str(ROOT / "scripts/start_project.py"), "init"], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/start_project.py"), "init",
                                 "--spec", str(ROOT / "examples/synthetic-intake.json"), "--output", str(self.output)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIs(json.loads(result.stderr)["production_ready"], False)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
