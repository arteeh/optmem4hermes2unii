# optmem4hermes2unii

A conversion script to move memory from [rarf's optmem plugin for Hermes](https://github.com/rarf/optmem-hermes-plugin)
(Hermes agent sessions + optmem notes) to [UniiChat](https://uniichat.com) (unii-chat, self-hosted `unii serve`).

The Hermes history goes **in front of** the Unii chat, in date order, written straight into the Unii store
(`$OPT_DIR/users/<id>`, e.g. `~/.opt/users/1`). It does not use Unii's `/import claude` / `/import codex`, which can only
add history *after* the current chat.

Studied on UniiChat v1.0.137 (its JS, read-only). Another Unii version may store things differently: dry-run first.

## Run

```
# 1. everything on a copy of the store, in ./out (the store itself is only read)
python3 -m optmem4hermes2unii dry-run --hermes ~/Projects/hermes/data --store ~/.opt/users/1 --out ./out

# 2. for real: stop Unii, apply (backup first), start Unii
systemctl --user stop unii-serve.service unii.service
python3 -m optmem4hermes2unii apply --hermes ~/Projects/hermes/data --store ~/.opt/users/1 --live
systemctl --user start unii-serve.service unii.service
# 3. in the web UI the import shows as paused, with its cost: press Resume (terminal: /import resume)

python3 -m optmem4hermes2unii verify --store ~/.opt/users/1   # check any store
```

Python 3.9+, no dependencies. Hermes' `state.db` is opened read-only.

`apply` refuses if any process has a file open in the store (Unii running), if the store is inside `$OPT_DIR`
(default `~/.opt`) without `--live`, if an import is under way, or if Hermes history is already in it. It copies the
store to `--backup` (default `~/.local/share/optmem4hermes2unii/backup-<id>-<time>/`) first, checks the result the way
Unii will read it, and restores the backup on any problem. To undo later: stop Unii, copy the backup back.

Options: `--max-tool-chars N` (cut tool results, default 4000, 0 = no cut), `--max-call-chars N` (cut long tool-call
arguments, default 2000), `--keep-gateway` (keep Hermes' Discord origin headers), `--with-log` / `--with-tree LEVEL`
(optmem `LOG.txt` / `TREE/LEVEL` as a small extra session at the end of the Hermes part), `--late keep|drop` (Hermes
messages dated after the first Unii message: keep them just before the Unii chat, or drop them), `--free-leaves` (see
cost), `--start` (summaries start as soon as Unii starts instead of paused), `--filler-summaries true|false`
(write the summary lines over the alignment fillers ourselves, free; default true), `--source-name NAME` and
`--agent-name NAME` (how the session notes and the boundary note name the app and the agent; default `Hermes`, `Eris`).
Yes/no options take `true|false` (or nothing, meaning true): e.g. `--free-leaves true --start false`.

## How it works

**Hermes side.** Hermes rewrites its history at every context compaction: old rows stay and a "demoted" copy of earlier
turns is inserted after them. The tool rebuilds the true conversation (first occurrence wins; copies, Hermes' own
compaction recaps and empty rows are dropped), in Hermes id order, each message with Hermes' own timestamp:

| Hermes | Unii kind |
|---|---|
| user message | `user` |
| assistant text | `unii` (with `imported: true`, so Unii shows it as `ai`: another AI's) |
| assistant tool call | `tool` (`ai` too): `<name> <compact JSON args>` |
| tool result, background-process notice | `echo` |

Each session starts with a `note` ("Hermes (Eris, discord) session in ..., from ... (imported)"). Texts over 30000
UTF-16 units are paged like Unii does (`more: true`). optmem's own summary tree does not line up with Unii's binary
tree, so it is not spliced in (it can be added as notes with `--with-tree`).

**Unii side** (from its `Memory` code):

- `main/<day>.jsonl` holds messages `{kind, text, imported?, i, size, date}`; `tree/<day>.jsonl` holds summary lines
  `{l, i, text, size}`, line `(l, i)` covering messages `i·2^l … (i+1)·2^l − 1`.
- `main.index` / `tree.index` (16-byte records) are rebuilt by Unii from the day files when missing, so the tool only
  writes JSONL and deletes them. `view.json` keeps the model pick and agents, gets its views renumbered and its scan
  positions reset.
- Hermes takes positions `0 … N−1`, the Unii chat moves to `N …`. `N` is padded with a few empty notes to a multiple of
  `2^L` (`L` = the highest level in Unii's tree) and ends with a boundary note saying where the chat starts (and that
  ids written in older messages and summaries are now off by `N`). So every existing summary line stays valid as
  `(l, i + N/2^l)`: **none is rewritten**. The lines over the padding are written by the tool (free).
- On start Unii queues every missing summary. A line whose last message is `imported` waits in the **import** queue,
  which runs only while an import batch is not paused; the tool writes that batch (`import.json`, paused by default),
  so the web UI shows the Hermes part as an import with its progress, cost and a Pause/Resume button.

**Cost.** Unii makes a one-line summary of every message and merges them pairwise up the tree, eagerly, not only past
the 128 KB view budget. A message or merge that fits in 512 bytes is copied for free; the rest costs a compactor call
(default `claude-api` haiku-5.5, on the Anthropic API key). The Hermes part is far past 128 KB, so it needs those
calls; the report gives Unii's own estimate. `--free-leaves` gives each long Hermes message its first 512 bytes as its
summary line (Unii's `line` field) instead of a model summary: cheaper, but those lines are just cut-off text (the
merges above them are still made by the model, which also sees the full messages).

## Output of dry-run

- `out/store/`: the copied store with the Hermes history in it (what `apply` would leave)
- `out/report.md`: counts, placement, late messages, cost estimate, checks, first records

Nothing in `out/` is committed (it holds your real conversations).
