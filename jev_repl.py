#!/usr/bin/env python3
"""A small Jev decision loop: choose an action, run it, observe, repeat.

Usage: python3 jev_repl.py REPO CONTRACT.json [--watch] [--step]

Contract format:
  {"goal": "...", "actions": [
    {"id": "look", "description": "Read the program", "kind": "read", "path": "app.py"},
    {"id": "edit", "description": "Apply a supplied change", "kind": "write",
     "path": "app.py", "content": "...", "required": true},
    {"id": "test", "description": "Run tests", "kind": "command",
     "argv": ["python3", "-m", "unittest"], "required": true}
  ]}

Jev selects only from supplied actions. This tool does not generate source code.
Commands in a contract run locally, so use contracts you trust.
"""

import argparse
import configparser
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"


def load_key():
    if os.environ.get("TYPESAFE_API_KEY"):
        return os.environ["TYPESAFE_API_KEY"]
    path = Path.home() / ".config/typesafe.env"
    if not path.exists():
        raise SystemExit("Set TYPESAFE_API_KEY or add it to ~/.config/typesafe.env")
    content = path.read_text().lstrip("\ufeff")
    if any(line.strip().startswith("[") for line in content.splitlines()):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
        for section in parser.sections():
            value = parser.get(section, "TYPESAFE_API_KEY", fallback="").strip().strip("\"'")
            if value:
                return value
    for line in content.splitlines():
        name, separator, value = line.removeprefix("export ").partition("=")
        if separator and name.strip() == "TYPESAFE_API_KEY":
            return value.strip().strip("\"'")
    raise SystemExit("TYPESAFE_API_KEY was not found in ~/.config/typesafe.env")


def inside(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise ValueError(f"Unsafe path: {name}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes repository: {name}")
    return path


def log(path, record):
    with path.open("a") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")


def choose(key, state, api_log):
    menu = state["available_actions"]
    payload = {"model": "jev-latest", "state": state,
               "questions": {"next_action": {"type": "choice",
                   "instructions": "Choose the next useful action. Finish only when the goal is met.",
                   "criteria": {item["id"]: item["description"] for item in menu}}}}
    log(api_log, {"direction": "request", "body": payload})
    request = urllib.request.Request(API_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
            log(api_log, {"direction": "response", "status": response.status, "body": body})
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        log(api_log, {"direction": "response", "status": error.code, "body": body})
        raise
    except Exception as error:
        log(api_log, {"direction": "response", "error": type(error).__name__, "message": str(error)})
        raise
    answer = body["answers"]["next_action"]
    choice = answer["choice"]
    if choice not in {item["id"] for item in menu}:
        raise ValueError(f"Jev chose an unavailable action: {choice}")
    return choice, answer.get("confidence")


def execute(root, action):
    kind = action["kind"]
    if kind == "read":
        path = inside(root, action["path"])
        return {"ok": path.is_file(), "output": path.read_text()[:6000] if path.is_file() else "File absent"}
    if kind == "write":
        path = inside(root, action["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(action["content"])
        return {"ok": True, "output": f"Wrote {action['path']}"}
    if kind == "command":
        argv = action["argv"]
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("command argv must be a nonempty string list")
        result = subprocess.run(argv, cwd=root, text=True, capture_output=True,
                                timeout=action.get("timeout_seconds", 30), check=False)
        return {"ok": result.returncode == 0, "exit_code": result.returncode,
                "output": (result.stdout + result.stderr)[-6000:]}
    raise ValueError(f"Unknown action kind: {kind}")


def run(root, contract, max_steps, log_dir, watch, step):
    root = root.resolve()
    if not root.is_dir():
        raise ValueError("Repository folder does not exist")
    plan = json.loads(contract.read_text())
    actions = plan["actions"]
    if len({item["id"] for item in actions}) != len(actions) or any(item["id"] == "finish" for item in actions):
        raise ValueError("Action IDs must be unique and cannot be 'finish'")
    for item in actions:
        if item["kind"] in ("read", "write"):
            inside(root, item["path"])
        elif item["kind"] != "command":
            raise ValueError(f"Unknown action kind: {item['kind']}")
    log_dir = log_dir.resolve() if log_dir else root / ".jev-repl"
    log_dir.mkdir(parents=True, exist_ok=True)
    api_log, progress = log_dir / "api.jsonl", log_dir / "progress.txt"
    api_log.write_text("")
    progress.write_text(f"Goal: {plan['goal']}\n")
    key = load_key()
    events, used, checks, revision = [], set(), {}, 0
    for number in range(1, max_steps + 1):
        available = []
        for item in actions:
            if item["kind"] == "write" and item["id"] in used:
                continue
            if item["kind"] == "read" and checks.get(item["id"], {}).get("revision") == revision:
                continue
            if item["kind"] == "command" and item.get("required") and checks.get(item["id"]) == {"revision": revision, "ok": True}:
                continue
            available.append({"id": item["id"], "description": item["description"]})
        available.append({"id": "finish", "description": "Declare the goal achieved"})
        state = {"goal": plan["goal"], "requirements": plan.get("requirements", []),
                 "revision": revision,
                 "recent_events": events[-5:], "available_actions": available}
        choice, confidence = choose(key, state, api_log)
        if choice == "finish":
            missing = [item["id"] for item in actions if item.get("required") and
                       (checks.get(item["id"]) != {"revision": revision, "ok": True}
                        if item["kind"] == "command" else item["id"] not in used)]
            observation = {"ok": not missing, "output": "Complete" if not missing else f"Required actions remain: {missing}"}
        else:
            action = next(item for item in actions if item["id"] == choice)
            observation = execute(root, action)
            used.add(choice)
            if action["kind"] == "write" and observation["ok"]:
                revision += 1
            checks[choice] = {"revision": revision, "ok": observation["ok"]}
        event = {"step": number, "choice": choice, "confidence": confidence,
                 "revision": revision, **observation}
        events.append(event)
        log(progress, event)
        if watch or step:
            print(f"{number:02d} {choice}: {observation['output'][:600]}", flush=True)
        if choice == "finish" and observation["ok"]:
            return 0
        if step:
            input("Press Enter for the next Jev decision...")
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", type=Path)
    parser.add_argument("contract", type=Path)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--step", action="store_true")
    args = parser.parse_args()
    return run(args.repo, args.contract, args.max_steps, args.log_dir, args.watch, args.step)


if __name__ == "__main__":
    sys.exit(main())
