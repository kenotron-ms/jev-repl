"""Jev-controlled repository REPL. Run `python3 jev_repl.py --help`."""

import argparse
import configparser
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

def api_key():
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    path = Path.home() / ".config" / "typesafe.env"
    if not path.exists():
        return None
    content = path.read_text().lstrip("\ufeff")
    if any(line.strip().startswith("[") for line in content.splitlines()):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
        for section in parser.sections():
            value = parser.get(section, "TYPESAFE_API_KEY", fallback="").strip().strip("\"'")
            if value:
                return value
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip() == "TYPESAFE_API_KEY":
                return value.strip().strip("\"'")
    return None


def run_command(argv, cwd, timeout=10):
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                                timeout=timeout, check=False)
        return {"exit_code": result.returncode, "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
                "seconds": round(time.monotonic() - started, 3)}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "stdout": "", "stderr": "Timed out",
                "seconds": round(time.monotonic() - started, 3)}


def safe_path(root, relative):
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] in (".git", ".jev-repl"):
        raise ValueError("Unsafe file path")
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Path escapes repository")
    return target


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def git(root, *args):
    return run_command(["git", *args], root)


class RepoRepl:
    def __init__(self, root, contract, max_steps, max_calls, watch=False, pause_seconds=0, step=False, log_file=None, api_log_file=None):
        self.root = root.resolve()
        self.contract = json.loads(contract.read_text())
        self.max_steps = max_steps
        self.max_calls = max_calls
        self.watch = watch
        self.pause_seconds = pause_seconds
        self.step = step
        self.steps = 0
        self.calls = 0
        self.revision = 0
        self.events = []
        self.last_seen = {}
        self.checks = {}
        self.tactics_used = set()
        self.file_ops_used = set()
        self.undo_stack = []
        self.touched_paths = set()
        self.finished = False
        self.checkpoint_revision = None
        self.finish_rejected = None
        self.run_dir = self.root / ".jev-repl"
        self.run_dir.mkdir(exist_ok=True)
        self.log_file = Path(log_file).resolve() if log_file else self.run_dir / "progress.md"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.write_text("# Jev repository run\n\nGoal: " + self.contract["goal"]
                                 + "\n\nContract:\n" + "".join("\n- " + item for item in self.contract["contracts"])
                                 + "\n\n---\n")
        self.api_log_file = Path(api_log_file).resolve() if api_log_file else self.run_dir / "api-exchanges.jsonl"
        self.api_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.api_log_file.write_text("")
        self.api_markdown_file = self.api_log_file.with_suffix(".md")
        self.api_markdown_file.write_text("# TypeSafe API exchanges\n\nRequest and response bodies are logged in full. The Authorization header is omitted.\n")
        top = git(self.root, "rev-parse", "--show-toplevel")
        if top["exit_code"] != 0 or Path(top["stdout"].strip()).resolve() != self.root:
            raise ValueError("Target must be a Git repository root")
        dirty = git(self.root, "status", "--porcelain", "--untracked-files=all")
        tracked_dirty = [line for line in dirty["stdout"].splitlines()
                         if not line.endswith(".jev-repl/") and ".jev-repl/" not in line]
        if tracked_dirty:
            raise ValueError("Start from a clean Git worktree; local changes were left untouched")
        self.initial_head = git(self.root, "rev-parse", "HEAD")["stdout"].strip()
        for tactic in self.contract["tactics"]:
            for name in tactic["writes"]:
                safe_path(self.root, name)
        for operation in self.contract.get("file_ops", []):
            if operation["kind"] not in ("write", "rename", "delete"):
                raise ValueError("Unknown file operation")
            for field in ("source", "target"):
                if field in operation:
                    safe_path(self.root, operation[field])
        for check in self.contract["checks"]:
            if not isinstance(check["argv"], list) or not check["argv"]:
                raise ValueError("Each check needs an argv list")

    def record(self, kind, data):
        event = {"step": self.steps, "revision": self.revision, "kind": kind, **data}
        self.events.append(event)
        with (self.run_dir / "trace.jsonl").open("a") as output:
            output.write(json.dumps(event, default=str) + "\n")

    def append_log(self, content):
        with self.log_file.open("a") as output:
            output.write(content + "\n")

    def append_api_log(self, record):
        with self.api_log_file.open("a") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
        label = "Request" if record["direction"] == "request" else "Response"
        with self.api_markdown_file.open("a") as output:
            output.write("\n## " + label + " " + str(record["request_id"]) + "\n\n")
            if "http_status" in record:
                output.write("HTTP status: " + str(record["http_status"]) + ". ")
            if "seconds" in record:
                output.write("Elapsed: " + str(record["seconds"]) + "s.\n\n")
            else:
                output.write("\n")
            output.write("```json\n" + json.dumps(record.get("body", record.get("error")), indent=2, ensure_ascii=False) + "\n```\n")

    def action_log(self, selected, event):
        kind = event["kind"]
        if kind in ("edit", "file_op"):
            self.append_log("### File transforms\n")
            for name in event["files"]:
                path = safe_path(self.root, name)
                tracked = git(self.root, "ls-files", "--error-unmatch", "--", name)["exit_code"] == 0
                detail = (git(self.root, "diff", "--", name)["stdout"] if tracked
                          else ("New file: " + name + "\n" + path.read_text() if path.exists() else "Removed: " + name))
                self.append_log("**" + name + "**\n\n```diff\n" + (detail[:6000].strip() or "No diff") + "\n```\n")
        elif kind == "run":
            self.append_log("### Command result\n\nCheck: `" + event["check"] + "` · exit code: `"
                            + str(event["exit_code"]) + "` · time: `" + str(event["seconds"])
                            + "s`\n\n```text\n" + (event["stdout"] or event["stderr"] or "No output").strip() + "\n```\n")
        elif kind == "finish":
            self.append_log("### Completion assessment\n\n" +
                            ("Accepted. Required checks and Git state satisfy the completion gate."
                             if event["accepted"] else "Rejected: " + event["reason"]) + "\n")
        elif kind == "git_checkpoint":
            self.append_log("### Git checkpoint\n\n```text\n" +
                            (event.get("stdout") or event.get("stderr") or "No output").strip() + "\n```\n")
        elif kind == "inspection":
            self.append_log("### Inspected " + event["file"] + "\n\n```python\n" +
                            (event["content"] or "File absent") + "\n```\n")
        else:
            self.append_log("### Observation\n\n```text\n" + str(event.get("stdout", event))[:4000] + "\n```\n")

    def menu(self):
        options = []
        for tactic in self.contract["tactics"]:
            if tactic["id"] not in self.tactics_used and all(
                    requirement in self.tactics_used for requirement in tactic.get("requires", [])):
                options.append({"id": "edit:" + tactic["id"], "kind": "edit",
                                "description": tactic["description"]})
        for operation in self.contract.get("file_ops", []):
            if operation["id"] not in self.file_ops_used:
                options.append({"id": "fileop:" + operation["id"], "kind": "file",
                                "description": operation["description"]})
        for name in self.contract["inspect_files"]:
            if self.last_seen.get("inspect:" + name) != self.revision:
                options.append({"id": "inspect:" + name, "kind": "inspect",
                                "description": "Read " + name + " to learn the current implementation"})
        for check in self.contract["checks"]:
            if self.checks.get(check["id"], {}).get("revision") != self.revision:
                options.append({"id": "run:" + check["id"], "kind": "run",
                                "description": check["description"]})
        for kind in ("git_status", "git_diff"):
            if self.last_seen.get(kind) != self.revision:
                options.append({"id": kind, "kind": "git", "description":
                                "Inspect repository status" if kind == "git_status" else "Inspect current diff"})
        if self.undo_stack:
            options.append({"id": "undo", "kind": "edit", "description": "Undo the last edit transaction"})
        if (self.contract.get("allow_git_checkpoint") and self.revision > 0
                and self.all_required_pass() and self.checkpoint_revision != self.revision):
            options.append({"id": "git_checkpoint", "kind": "git",
                            "description": "Stage changed task files and make a local checkpoint commit"})
        evidence = (self.revision, self.checkpoint_revision,
                    tuple(sorted((k, v.get("revision"), v.get("exit_code"))
                                                 for k, v in self.checks.items())))
        if evidence != self.finish_rejected:
            options.append({"id": "finish", "kind": "control",
                            "description": "Declare the contract achieved; the harness will verify required checks"})
        return options

    def all_required_pass(self):
        return all(self.checks.get(check["id"], {}).get("revision") == self.revision
                   and self.checks[check["id"]]["exit_code"] == 0
                   for check in self.contract["checks"] if check.get("required", True))

    def completion_ready(self):
        if not self.all_required_pass():
            return False
        if not all(tactic in self.tactics_used for tactic in self.contract.get("required_tactics", [])):
            return False
        if self.contract.get("require_git_checkpoint"):
            if self.checkpoint_revision != self.revision:
                return False
            status = git(self.root, "status", "--porcelain", "--untracked-files=all")
            if status["exit_code"] != 0 or any(".jev-repl/" not in line for line in status["stdout"].splitlines()):
                return False
        return True

    def state(self, menu):
        files = []
        for name in self.contract["inspect_files"]:
            path = safe_path(self.root, name)
            if path.exists():
                files.append({"path": name, "sha256": file_hash(path),
                              "source": path.read_text()[:5000]})
        return {"goal": self.contract["goal"], "contracts": self.contract["contracts"],
                "current_files": files, "checks": self.checks,
                "recent_events": self.events[-6:],
                "available_actions": menu,
                "instruction": "Choose the next useful action. Running checks is your decision. Finish only when evidence supports the contract."}

    def choose(self, menu):
        key = api_key()
        if not key:
            raise RuntimeError("TYPESAFE_API_KEY is missing")
        if self.calls >= self.max_calls:
            raise RuntimeError("Jev call budget exhausted")
        self.calls += 1
        payload = {"model": "jev-latest", "state": self.state(menu),
                   "questions": {"next_action": {"type": "choice",
                       "instructions": "Select the most useful next action toward satisfying the contracts. Consider architecture, prior observations, uncertainty, and test evidence. Select one listed action ID.",
                       "criteria": {item["id"]: item["description"] for item in menu}}}}
        request_id = self.calls
        self.append_api_log({"request_id": request_id, "direction": "request",
                             "method": "POST", "url": "https://api.typesafe.ai/v1/systemone",
                             "headers": {"Content-Type": "application/json"}, "body": payload})
        request = urllib.request.Request(
            "https://api.typesafe.ai/v1/systemone", data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read()
                status = response.status
            result = json.loads(raw)
            self.append_api_log({"request_id": request_id, "direction": "response",
                                 "http_status": status, "seconds": round(time.monotonic() - started, 3),
                                 "body": result})
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = raw[:4000]
            self.append_api_log({"request_id": request_id, "direction": "response",
                                 "http_status": error.code, "seconds": round(time.monotonic() - started, 3),
                                 "body": body})
            raise
        except Exception as error:
            self.append_api_log({"request_id": request_id, "direction": "response",
                                 "seconds": round(time.monotonic() - started, 3),
                                 "error": {"type": type(error).__name__, "message": str(error)[:500]}})
            raise
        answer = result["answers"]["next_action"]
        selected = answer["choice"]
        if selected not in {item["id"] for item in menu}:
            raise ValueError("Jev selected an unavailable action")
        decision = {"selected": selected, "confidence": answer["confidence"],
                    "probabilities": answer["probabilities"],
                    "input_tokens": result.get("usage", {}).get("input_tokens"),
                    "seconds": round(time.monotonic() - started, 3)}
        self.record("decision", decision)
        return selected

    def apply_tactic(self, tactic):
        before = {}
        for name, content in tactic["writes"].items():
            path = safe_path(self.root, name)
            before[name] = path.read_bytes() if path.exists() else None
        try:
            for name, content in tactic["writes"].items():
                path = safe_path(self.root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        except Exception:
            for name, original in before.items():
                path = safe_path(self.root, name)
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            raise
        self.undo_stack.append(("tactic", tactic["id"], before))
        self.touched_paths.update(before)
        self.tactics_used.add(tactic["id"])
        self.revision += 1
        self.record("edit", {"tactic": tactic["id"], "files": list(tactic["writes"])})

    def apply_file_op(self, operation):
        names = [operation[field] for field in ("source", "target") if field in operation]
        before = {name: safe_path(self.root, name).read_bytes()
                  if safe_path(self.root, name).exists() else None for name in names}
        kind = operation["kind"]
        if kind == "write":
            target = safe_path(self.root, operation["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(operation["content"])
        elif kind == "rename":
            source = safe_path(self.root, operation["source"])
            target = safe_path(self.root, operation["target"])
            if not source.is_file() or target.exists():
                raise ValueError("Rename requires an existing source and absent target")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        elif kind == "delete":
            source = safe_path(self.root, operation["source"])
            if not source.is_file():
                raise ValueError("Delete requires an existing file")
            source.unlink()
        self.undo_stack.append(("fileop", operation["id"], before))
        self.touched_paths.update(before)
        self.file_ops_used.add(operation["id"])
        self.revision += 1
        self.record("file_op", {"operation": operation["id"], "kind": kind, "files": names})

    def undo(self):
        origin, action_id, before = self.undo_stack.pop()
        for name, original in before.items():
            path = safe_path(self.root, name)
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        if origin == "tactic":
            self.tactics_used.discard(action_id)
        else:
            self.file_ops_used.discard(action_id)
        self.revision += 1
        self.record("undo", {"files": list(before)})

    def act(self, selected):
        self.steps += 1
        if selected.startswith("edit:"):
            tactic = next(item for item in self.contract["tactics"] if item["id"] == selected[5:])
            self.apply_tactic(tactic)
        elif selected.startswith("fileop:"):
            operation = next(item for item in self.contract.get("file_ops", [])
                             if item["id"] == selected[7:])
            self.apply_file_op(operation)
        elif selected.startswith("inspect:"):
            name = selected[8:]
            path = safe_path(self.root, name)
            self.last_seen[selected] = self.revision
            self.record("inspection", {"file": name, "content": path.read_text()[:5000] if path.exists() else None})
        elif selected.startswith("run:"):
            check = next(item for item in self.contract["checks"] if item["id"] == selected[4:])
            argv = [sys.executable if arg == "{python}" else arg for arg in check["argv"]]
            result = run_command(argv, self.root, timeout=check.get("timeout_seconds", 10))
            self.checks[check["id"]] = {"revision": self.revision, **result}
            self.record("run", {"check": check["id"], **result})
        elif selected == "git_status":
            self.last_seen[selected] = self.revision
            self.record("git_status", git(self.root, "status", "--short"))
        elif selected == "git_diff":
            self.last_seen[selected] = self.revision
            self.record("git_diff", git(self.root, "diff", "--", ".", ":!.jev-repl"))
        elif selected == "git_checkpoint":
            paths = []
            for name in sorted(self.touched_paths):
                if safe_path(self.root, name).exists() or git(self.root, "ls-files", "--error-unmatch", "--", name)["exit_code"] == 0:
                    paths.append(name)
            staged = git(self.root, "add", "-A", "--", *paths) if paths else {"exit_code": 1, "stderr": "No touched files"}
            committed = git(self.root, "commit", "-m", "Jev REPL contract checkpoint") if staged["exit_code"] == 0 else staged
            self.record("git_checkpoint", committed)
            if committed["exit_code"] == 0:
                self.checkpoint_revision = self.revision
        elif selected == "undo":
            self.undo()
        elif selected == "finish":
            if self.completion_ready():
                self.finished = True
                self.record("finish", {"accepted": True})
            else:
                evidence = (self.revision, self.checkpoint_revision,
                            tuple(sorted((k, v.get("revision"), v.get("exit_code"))
                                                         for k, v in self.checks.items())))
                self.finish_rejected = evidence
                self.record("finish", {"accepted": False,
                                       "reason": "Required tactics, checks, or Git checkpoint are missing on the current revision"})
        else:
            raise ValueError("Unknown action")

    def loop(self):
        self.record("start", {"initial_head": self.initial_head})
        if self.watch:
            print("GOAL: " + self.contract["goal"], flush=True)
            print("REPOSITORY: " + str(self.root), flush=True)
        while self.steps < self.max_steps and not self.finished:
            menu = self.menu()
            if not menu:
                break
            try:
                selected = self.choose(menu)
                decision = self.events[-1]
                ranking = sorted(decision["probabilities"].items(), key=lambda item: item[1], reverse=True)[:3]
                self.append_log("\n## Step " + str(self.steps + 1) + ": `" + selected + "`\n\n"
                                + "Jev confidence: **" + f"{decision['confidence']:.2f}" + "**. "
                                + "Other leading options: " + ", ".join("`" + name + "` " + f"{probability:.2f}" for name, probability in ranking)
                                + ".\n")
                if self.watch:
                    print("\nJEV → " + selected + f"  confidence={decision['confidence']:.2f}", flush=True)
                    print("Top choices: " + ", ".join(f"{name} {probability:.2f}" for name, probability in ranking), flush=True)
                self.act(selected)
            except Exception as error:
                self.record("error", {"type": type(error).__name__, "message": str(error)[:500]})
                self.append_log("\n## Run error\n\n" + type(error).__name__ + ": " + str(error)[:500] + "\n")
                if self.watch:
                    print(f"ERROR: {type(error).__name__}: {error}", flush=True)
                break
            last = self.events[-1]
            self.action_log(selected, last)
            if self.watch:
                if last["kind"] == "edit":
                    print("Edited: " + ", ".join(last["files"]), flush=True)
                    for name in last["files"]:
                        path = safe_path(self.root, name)
                        tracked = git(self.root, "ls-files", "--error-unmatch", "--", name)["exit_code"] == 0
                        detail = (git(self.root, "diff", "--", name)["stdout"] if tracked
                                  else ("New file: " + name + "\n" + path.read_text() if path.exists() else "Removed: " + name))
                        print(detail[:1600].strip(), flush=True)
                elif last["kind"] == "run":
                    print(f"Check exit={last['exit_code']} ({last['seconds']}s)", flush=True)
                    print((last["stdout"] or last["stderr"] or "No output").strip(), flush=True)
                elif last["kind"] == "finish":
                    print("Finish accepted" if last["accepted"] else "Finish rejected: " + last["reason"], flush=True)
                elif last["kind"] == "git_checkpoint":
                    print("Git checkpoint: " + (last.get("stdout") or last.get("stderr") or "No output").strip(), flush=True)
                elif "content" in last:
                    print((last["content"] or "File absent")[:1200], flush=True)
                else:
                    print(last["kind"] + ": " + str(last.get("stdout", last.get("files", "")))[:1200], flush=True)
                if self.step and not self.finished:
                    try:
                        input("Press Enter for Jev's next decision...")
                    except EOFError:
                        pass
                elif self.pause_seconds and not self.finished:
                    time.sleep(self.pause_seconds)
            else:
                print(f"{self.steps:02d} {selected}: {last['kind']}", flush=True)
                if self.pause_seconds and not self.finished:
                    time.sleep(self.pause_seconds)
        summary = {"finished": self.finished, "steps": self.steps, "jev_calls": self.calls,
                   "checks": self.checks, "root": str(self.root),
                   "trace": str(self.run_dir / "trace.jsonl"),
                   "api_exchanges_jsonl": str(self.api_log_file),
                   "api_exchanges_markdown": str(self.api_markdown_file)}
        (self.run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        self.append_log("\n## Run summary\n\nFinished: **" + str(self.finished).lower()
                        + "**. Steps: **" + str(self.steps) + "**. Jev calls: **"
                        + str(self.calls) + "**.\n")
        print(json.dumps(summary, indent=2))
        return 0 if self.finished else 1


def demo_init(root):
    if root.exists() and any(root.iterdir()):
        raise ValueError("Demo target must be empty")
    root.mkdir(parents=True, exist_ok=True)
    (root / "counter.py").write_text("def next_count(value):\n    return value + 1\n")
    (root / "probe.py").write_text("from counter import next_count\nprint(next_count(4))\n")
    (root / "check.py").write_text(
        "from counter import next_count\n"
        "assert next_count(4) == 5\n"
        "assert next_count(4, 3) == 7\n"
        "print('contract passes')\n")
    contract = {
        "goal": "Add an optional step size to the working counter without changing its default behavior.",
        "contracts": ["next_count(4) == 5", "next_count(4, 3) == 7"],
        "inspect_files": ["counter.py", "probe.py", "check.py"],
        "checks": [
            {"id": "probe", "description": "Run the working primitive with Python",
             "argv": ["{python}", "probe.py"], "required": False},
            {"id": "contract", "description": "Check default and custom steps",
             "argv": ["{python}", "check.py"], "required": True},
        ],
        "tactics": [{"id": "add_step", "description": "Add an optional step parameter, preserving the default of one",
                     "writes": {"counter.py": "def next_count(value, step=1):\n    return value + step\n"}}],
        "required_tactics": ["add_step"],
        "allow_git_checkpoint": True,
        "require_git_checkpoint": True,
    }
    (root / "contract.json").write_text(json.dumps(contract, indent=2) + "\n")
    (root / ".gitignore").write_text(".jev-repl/\n__pycache__/\n*.pyc\n")
    for command in (["git", "init", "-q"],
                    ["git", "config", "user.name", "Jev Demo"],
                    ["git", "config", "user.email", "jev-demo@example.invalid"],
                    ["git", "add", "."], ["git", "commit", "-qm", "Working seed"]):
        result = run_command(command, root)
        if result["exit_code"] != 0:
            raise RuntimeError(result["stderr"])
    print(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("demo-init")
    init.add_argument("root", type=Path)
    run = sub.add_parser("run")
    run.add_argument("root", type=Path)
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--max-steps", type=int, default=30)
    run.add_argument("--max-calls", type=int, default=30)
    run.add_argument("--watch", action="store_true", help="Print Jev decisions and action results")
    run.add_argument("--pause-seconds", type=float, default=0, help="Delay between watched turns")
    run.add_argument("--step", action="store_true", help="Wait for Enter after each watched turn")
    run.add_argument("--log-file", type=Path, help="Readable Markdown log updated after every action")
    run.add_argument("--api-log-file", type=Path, help="Raw TypeSafe request/response JSONL; a Markdown copy is also written")
    args = parser.parse_args()
    if args.command == "demo-init":
        demo_init(args.root)
        return 0
    return RepoRepl(args.root, args.contract, args.max_steps, args.max_calls,
                    watch=args.watch or args.step, pause_seconds=args.pause_seconds,
                    step=args.step, log_file=args.log_file,
                    api_log_file=args.api_log_file).loop()


if __name__ == "__main__":
    sys.exit(main())
