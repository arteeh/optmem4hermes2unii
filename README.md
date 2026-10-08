# optmem4hermes2unii

A conversion script to move memory from [rarf's optmem plugin for Hermes](https://github.com/rarf/optmem-hermes-plugin)
(Hermes agent sessions + optmem notes) to [UniiChat](https://uniichat.com) (unii-chat).

**Status: dry run only.** It reads Hermes' `state.db` read-only, converts, validates, and writes to a separate
output dir. It never writes to the Unii store (`~/.opt/users/1`) or to Hermes. `apply` is a stub that refuses.

## Run

```
python3 -m optmem4hermes2unii dry-run --hermes ~/Projects/hermes/data --out ./out
```

Python 3.9+, no dependencies. Options: `--max-tool-chars N` (cut tool results, default 4000, 0 = no cut),
`--max-call-chars N` (cut long string args of tool calls, default 2000), `--keep-gateway` (keep Hermes' Discord
"Gateway message origin" / "Triggering message id" headers), `--with-log` (optmem `LOG.txt` as a small session
of Eris' notes), `--with-tree LEVEL` (optmem `TREE/LEVEL` summaries as a small session), `--unii-store PATH`
(read for scale in the report only).

## What it does

Hermes rewrites its history at every context compaction: old rows stay and a "demoted" copy of earlier turns is
inserted after them. The tool rebuilds the true conversation (first occurrence wins; copies, Hermes' own compaction
summaries and empty rows are dropped) in Hermes id order, then maps it to Unii's import format:

| Hermes | Unii kind |
|---|---|
| user message | `user` |
| assistant text | `unii` (shown by Unii as *ai* when imported) |
| assistant tool call | `tool`: `<name> <compact JSON args>` |
| tool result, background-process notice | `echo` |

Each Hermes session becomes one imported session (a `note` header, then its messages), each message carrying
Hermes' own timestamp as its `date`. Optmem trees do **not** align with Unii's binary summary tree, so summaries
are not spliced: Unii's compactor builds its own tree from the imported messages.

Output in `--out`:

- `unii-import.jsonl`: the body Unii's importer reads (`{kind,text,date,more}` lines, a `note` per session)
- `claude-config/projects/-hermes-eris/<session>.jsonl`: the same sessions as Claude Code session files, which
  Unii's existing `/import claude` reads (point `CLAUDE_CONFIG_DIR` at `claude-config`); label there says "Claude"
- `report.md`: counts, order and timestamp checks, sizes vs. the live store, Unii's own cost guess, samples

The report checks: Hermes ids and dates ordered per session, sessions ordered, every Hermes row accounted for, and
that a port of Unii's Claude-session parser turns the generated files back into exactly `unii-import.jsonl`.

Nothing in `out/` is committed (it holds your real conversations).
