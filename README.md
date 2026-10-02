# jev-repl

A small loop that asks Jev to choose an action, runs it locally, then sends the result back for the next choice. Jev chooses from actions in your JSON contract; the tool does not generate source code on its own.

## Run

Requires Python 3.9+ and a TypeSafe API key. The tool reads `TYPESAFE_API_KEY` from your environment or `~/.config/typesafe.env`.

```sh
cd /Users/ken/workspace/jev-repl
python3 jev_repl.py /path/to/project /path/to/contract.json --watch --step
```

`--watch` prints each choice and result. `--step` waits for Enter between choices; omit it to run continuously. The tool writes a readable trace to `/path/to/project/.jev-repl/progress.txt` and full API request/response records to `/path/to/project/.jev-repl/api.jsonl`. The API key is omitted from the log, but file contents returned by `read` actions can appear there.

Run `python3 jev_repl.py --help` for the contract format and other options. Contracts can offer `read`, `write`, and `command` actions. Required actions gate Jev's `finish` choice. Commands execute on your computer, so use contracts you trust.
