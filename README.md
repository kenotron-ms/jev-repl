# jev-repl

This folder contains one standalone tool, `jev_repl.py`. It calls the TypeSafe
Jev API to choose one action at a time, executes that action in a local Git
repository, and returns the result on the next call. It uses Python's standard
library and the `git` command; it does not call an LLM or contain a React app
generator.

## Try the small demo

```sh
python3 jev_repl.py demo-init /tmp/jev-counter-demo
python3 jev_repl.py run /tmp/jev-counter-demo \
  --contract /tmp/jev-counter-demo/contract.json --watch --step
```

The demo starts with a working counter. Its contract offers one explicit code
change that adds an optional step parameter, plus Python probes and checks.
Jev decides when to inspect, edit, run, checkpoint, and finish. `--step` pauses
between decisions; omit it for an uninterrupted run. Each run makes billable
TypeSafe API calls.

Open `/tmp/jev-counter-demo/.jev-repl/progress.md` to watch the decisions and
file diffs. Full request/response pairs go to `api-exchanges.jsonl` and a
readable `api-exchanges.md` in the same folder. The API key and Authorization
header are omitted from those logs. Source and contract text are included, so
inspect the logs before sharing them.

The tool reads `TYPESAFE_API_KEY` from the environment or
`~/.config/typesafe.env` (INI sections or `KEY=value`). It requires a clean Git
repository root. Git actions are local; the tool does not push or open PRs.
The TypeSafe request includes excerpts from the files named in your contract.
Run it only on source you intend to send to that API. Contract checks are local
commands, so use contracts you trust.

## Try your own repository

Run:

```sh
python3 jev_repl.py run /absolute/path/to/repo \
  --contract /absolute/path/to/contract.json --watch
```

The contract declares a goal, observable files, executable checks, and optional
edit tactics or file operations. Each tactic contains the exact source it can
write; Jev chooses among the actions you provide. A tactic may list `requires`
to expose lower-level actions after an earlier choice. The tool also offers
inspection, Git status/diff, undo, a local checkpoint, and a finish decision.
See the demo's generated `contract.json` for the format.

**Scope:** Jev makes decisions over a bounded action menu. The tool does not
invent arbitrary source code or perform semantic AST edits unless you supply
actions that do so. This is a clean decision-loop primitive for experimenting
with action providers, rather than a general coding agent.
