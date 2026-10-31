#!/usr/bin/env python3
"""Independent, offline acceptance for the company demo; standard library only.

Starts only its own server, uses an isolated database, exercises real HTTP, and
reports frontend source checks separately from browser evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class Acceptance:
    def __init__(self, root: Path, temporary: Path):
        self.root = root
        self.temporary = temporary
        self.database = temporary / "acceptance.sqlite3"
        self.process = None
        self.log = None
        self.base = ""
        self.checks = []
        self.starts = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check(self, name, condition, evidence):
        self.checks.append({"name": name, "passed": bool(condition), "evidence": evidence})

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def start(self):
        self.starts += 1
        log_path = self.temporary / f"server-{self.starts}.log"
        self.log = log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(self.root / "app.py"), "--host", "127.0.0.1",
             "--port", "0", "--db", str(self.database)],
            cwd=self.root, stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    address = json.loads(line)
                    if not isinstance(address, dict):
                        continue
                    port = address.get("port")
                    if port is None and isinstance(address.get("url"), str):
                        port = urllib.parse.urlsplit(address["url"]).port
                    if isinstance(port, int) and 0 < port < 65536:
                        self.base = f"http://127.0.0.1:{port}"
                        status, body, _ = self.request("GET", "/health")
                        if status == 200 and body == {"ok": True}:
                            self.check(f"server_start_{self.starts}", True,
                                       {"host": "127.0.0.1", "port": port,
                                        "isolated_database": True})
                            return
                except (ValueError, OSError, urllib.error.URLError):
                    pass
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError("Server startup failed: " + log_path.read_text(
            encoding="utf-8", errors="replace")[-2000:])

    def request(self, method, path, payload=None, raw=None):
        data = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(text)
            except ValueError:
                body = text
            return response.status, body, response.headers.get("Content-Type", "")

    def error(self, name, method, path, expected, payload=None, raw=None):
        status, body, mime = self.request(method, path, payload=payload, raw=raw)
        self.check(name, status == expected and isinstance(body, dict)
                   and isinstance(body.get("error"), str) and bool(body["error"])
                   and "application/json" in mime,
                   {"status": status, "expected_status": expected, "body": body})

    def run_http(self):
        self.start()
        status, body, mime = self.request("GET", "/health")
        self.check("health", status == 200 and body == {"ok": True}
                   and "application/json" in mime, {"status": status, "body": body})
        status, body, _ = self.request("GET", "/api/tasks")
        self.check("empty_database", status == 200 and body == {"tasks": []},
                   {"status": status, "body": body})

        created = []
        for name, title in (
            ("trim_title", "  ordinary task  "),
            ("xss_title", '<img src=x onerror="alert(1)">'),
            ("sql_title", "'); DROP TABLE tasks; --"),
            ("maximum_title", "x" * 120),
            ("minimum_title", "a"),
        ):
            status, body, mime = self.request("POST", "/api/tasks", {"title": title})
            task = body.get("task", {}) if isinstance(body, dict) else {}
            valid = (status == 201 and isinstance(task, dict) and "id" in task
                     and task.get("title") == title.strip() and task.get("status") == "todo"
                     and "application/json" in mime)
            self.check(name, valid, {"status": status, "body": body})
            if valid:
                created.append(task)
        if len(created) != 5:
            raise RuntimeError("Cannot verify dependent updates and persistence: creation failed")

        task_path = "/api/tasks/" + urllib.parse.quote(str(created[0]["id"]), safe="")
        for state in ("done", "todo", "done"):
            status, body, _ = self.request("PATCH", task_path, {"status": state})
            task = body.get("task", {}) if isinstance(body, dict) else {}
            self.check("status_transition_" + state + "_" + str(len(self.checks)),
                       status == 200 and task.get("status") == state
                       and task.get("id") == created[0]["id"]
                       and task.get("title") == created[0]["title"],
                       {"status": status, "body": body})
        created[0]["status"] = "done"

        for name, payload in (
            ("missing_title", {}), ("empty_title", {"title": ""}),
            ("whitespace_title", {"title": " \t "}),
            ("oversized_title", {"title": "x" * 121}),
            ("null_title", {"title": None}), ("numeric_title", {"title": 1}),
            ("array_body", []),
        ):
            self.error(name, "POST", "/api/tasks", 400, payload=payload)
        for name, payload in (("invalid_status", {"status": "waiting"}),
                              ("missing_status", {}), ("null_status", {"status": None})):
            self.error(name, "PATCH", task_path, 400, payload=payload)
        self.error("missing_task", "PATCH", "/api/tasks/999999999", 404,
                   payload={"status": "done"})
        self.error("malformed_create_json", "POST", "/api/tasks", 400, raw=b'{"title":')
        self.error("malformed_update_json", "PATCH", task_path, 400, raw=b'{"status":')
        self.error("unknown_route", "GET", "/not-a-route", 404)
        self.error("source_not_served", "GET", "/app.py", 404)
        for index, path in enumerate(("/../app.py", "/%2e%2e/app.py", "/web/../app.py",
                                      "/%2e%2e%2fapp.py", "/web/index.html")):
            status, body, _ = self.request("GET", path)
            self.check(f"static_allowlist_{index}", status in (400, 404),
                       {"path": path, "status": status, "body": body})
        for path in ("/", "/app.js", "/style.css"):
            status, body, mime = self.request("GET", path)
            expected = (self.root / "web" / ("index.html" if path == "/" else path[1:]))
            self.check("static_" + ("index" if path == "/" else path[1:]),
                       status == 200 and isinstance(body, str)
                       and body == expected.read_text(encoding="utf-8"),
                       {"path": path, "status": status, "content_type": mime,
                        "matches_source": isinstance(body, str)
                        and body == expected.read_text(encoding="utf-8")})

        status, before, _ = self.request("GET", "/api/tasks")
        expected_tasks = {str(task["id"]): task for task in created}
        actual_tasks = {str(task["id"]): task for task in before.get("tasks", [])}
        self.check("stored_titles_and_states", status == 200 and actual_tasks == expected_tasks,
                   {"status": status, "body": before})
        self.stop()
        self.start()
        status, after, _ = self.request("GET", "/api/tasks")
        self.check("restart_persistence", status == 200 and after == before,
                   {"status": status, "same_database": True, "body": after})

    def run_frontend_structure(self):
        html = (self.root / "web/index.html").read_text(encoding="utf-8")
        script = (self.root / "web/app.js").read_text(encoding="utf-8")
        css = (self.root / "web/style.css").read_text(encoding="utf-8")
        self.check("frontend_assets_linked", "app.js" in html and "style.css" in html,
                   {"kind": "source_structure", "browser_execution_tested": False})
        self.check("frontend_api_and_mutations", "/api/tasks" in script and "fetch(" in script
                   and "POST" in script and "PATCH" in script,
                   {"kind": "source_structure", "browser_execution_tested": False})
        self.check("frontend_text_rendering", "textContent" in script
                   and "innerHTML" not in script and "insertAdjacentHTML" not in script
                   and "document.write" not in script,
                   {"kind": "conservative_source_check", "browser_execution_tested": False,
                    "limitation": "Does not execute JavaScript or prove DOM behavior or XSS safety."})
        self.check("frontend_style_present", bool(css.strip()),
                   {"kind": "source_structure", "browser_execution_tested": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="company-demo-acceptance-") as directory:
        verifier = Acceptance(root, Path(directory))
        try:
            verifier.run_http()
        except Exception as error:
            verifier.check("http_suite_completed", False,
                           {"error": type(error).__name__, "message": str(error)})
        finally:
            verifier.stop()
        try:
            verifier.run_frontend_structure()
        except Exception as error:
            verifier.check("frontend_structure_completed", False,
                           {"error": type(error).__name__, "message": str(error)})
        report = {
            "schema_version": 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "passed": bool(verifier.checks) and all(item["passed"] for item in verifier.checks),
            "model_execution_tested": False,
            "prod_deployed": False,
            "browser_execution_tested": False,
            "scope": "Real local HTTP and SQLite restart acceptance; frontend source checks only.",
            "limitations": ["No real model backend invoked.", "No production deployment tested.",
                            "No browser, visual layout, accessibility, or DOM interaction tested."],
            "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                              for name in ("app.py", "web/index.html", "web/app.js", "web/style.css")
                              if (root / name).is_file()},
            "tests": verifier.checks,
        }
        report["test_count"] = len(report["tests"])
        report["passed_count"] = sum(item["passed"] for item in report["tests"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "test_count": report["test_count"],
                          "passed_count": report["passed_count"], "report": str(args.output)}))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
