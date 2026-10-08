"""optmem4hermes2unii: move Hermes (+ rarf's optmem plugin) memory to UniiChat. Dry run only."""
import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

from . import hermes, optmem, unii


def store_facts(path):
    """Read-only look at the live Unii store, for scale only."""
    p = Path(path).expanduser()
    mains = sorted((p / "main").glob("*.jsonl"))
    if not mains:
        return None
    n = b = 0
    for f in mains:
        data = f.read_bytes()
        b += len(data)
        n += data.count(b"\n")
    t = sum(f.stat().st_size for f in (p / "tree").glob("*.jsonl"))
    return dict(messages=n, main_bytes=b, tree_bytes=t)


def dry_run(a):
    out = Path(a.out).expanduser()
    if a.hermes_data_is_inside_out(out):
        raise SystemExit("refusing: --out may not be inside the Hermes data or the Unii store")
    sessions, st = hermes.load(a.hermes, keep_gateway=a.keep_gateway)
    hermes.assign_dates(sessions, st)
    opt = dict(max_tool_chars=a.max_tool_chars, max_call_chars=a.max_call_chars)
    outs, esc = unii.build(sessions, opt)
    extras = {}
    if a.with_log:
        s, info = optmem.log_session(a.hermes)
        if s:
            outs.append(s)
            extras["LOG.txt"] = info
    if a.with_tree:
        s, info = optmem.tree_session(a.hermes, a.with_tree)
        outs.append(s)
        extras[f"TREE/{a.with_tree}"] = info
    outs.sort(key=lambda s: (s.start, s.id))

    if out.exists():
        if any(out.iterdir()) and not (out / "unii-import.jsonl").exists():
            raise SystemExit(f"refusing: {out} exists, is not empty and is not an earlier dry-run output")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    n_body = unii.write_body(outs, out / "unii-import.jsonl")
    n_claude = unii.write_claude(outs, out / "claude-config")

    # ---- checks
    problems = []
    body = {}
    for sid, line in unii.body_lines(outs):
        body.setdefault(sid, []).append(line)
    back = unii.read_claude(out / "claude-config")
    for s in outs:
        mine = [x for x in body[s.id] if x["kind"] != "note"]
        if mine != back.get(s.id):
            problems.append(f"round trip differs for session {s.id}")
        dates = [x["date"] for x in mine]
        if dates != sorted(dates):
            problems.append(f"dates not ordered in {s.id}")
        hids = [o.hid for o in s.msgs]
        if not s.id.startswith("optmem") and hids != sorted(hids):
            problems.append(f"Hermes ids not ordered in {s.id}")
    starts = [s.start for s in outs]
    if starts != sorted(starts):
        problems.append("sessions not ordered by start")
    overlaps = [(a_.id, b_.id) for a_, b_ in zip(outs, outs[1:]) if a_.msgs[-1].date > b_.start]
    kept_ids = {m.hid for s in sessions for m in s.msgs}
    skipped = sum(st[k] for k in ("session_meta", "summaries", "hidden_empty", "dup_user", "dup_call", "dup_result", "dup_text"))
    if len(kept_ids) + skipped != st["rows"]:
        problems.append(f"row accounting: {len(kept_ids)} kept + {skipped} skipped != {st['rows']}")

    sizes = [unii.size(x["kind"], x["text"]) for lines in body.values() for x in lines]
    live = store_facts(a.unii_store)
    usd = unii.guess_usd(live["messages"] if live else 0, sizes)
    kinds = Counter(x["kind"] for lines in body.values() for x in lines)

    R = []
    w = R.append
    w("# optmem4hermes2unii: dry run\n")
    w(f"Source: {a.hermes}/state.db (read-only). Output: {out} (nothing else was written).\n")
    w("## Hermes rows -> messages")
    w(f"- rows read: {st['rows']}; messages kept: {len(kept_ids)} rows -> {sum(len(s.msgs) for s in outs if not s.id.startswith('optmem'))} Unii messages (one row can hold text + several tool calls)")
    w(f"- dropped as compaction copies: {st['dup_user']} user, {st['dup_call']} tool-call, {st['dup_result']} tool-result, {st['dup_text']} assistant-text rows")
    w(f"- dropped other: {st['session_meta']} session_meta, {st['summaries']} Hermes compaction summaries, {st['hidden_empty']} empty assistant rows")
    w(f"- kept specially: {st['notification']} background-process notices (as echo), {st['steer']} steer messages (as user), {st['failed_turn']} failed turn text, "
      f"{st['tool_calls']} tool calls; {st['gateway_stripped']} gateway-origin headers stripped; {esc} texts escaped; {st['demoted_only']} results that exist only as compaction stubs")
    w(f"- tool results cut to {a.max_tool_chars} chars, tool-call string args to {a.max_call_chars} (0 = no cut)\n")
    w("## Sessions (import order)")
    w("| session | where | messages | kinds | first | last |\n|---|---|---|---|---|---|")
    for s in outs:
        c = Counter(o.kind for o in s.msgs)
        w(f"| {s.id} | {s.where.split('/')[1]} | {len(s.msgs)} | {dict(c)} | {s.msgs[0].date} | {s.msgs[-1].date} |")
    w("")
    w("## Order and timestamps")
    w("- order = Hermes row id (append order), kept strictly; sessions sorted by start")
    w(f"- every Unii message carries Hermes' own timestamp as its `date` (Unii stores a `date` per message)")
    w(f"- timestamps that ran backwards vs. id order and were clamped to the previous date: {st['clamped']} (largest {st['max_clamp_s']} s)")
    w(f"- messages sharing the same millisecond as their predecessor (ties, order kept by position): {st['ties']}")
    w(f"- session overlaps in time: {overlaps or 'none'}")
    w(f"- extras: {extras or 'none (use --with-log / --with-tree N)'}")
    w(f"- checks: {'ALL OK (ids ordered, dates ordered, sessions ordered, row accounting, round trip body == parsed Claude-style files)' if not problems else 'PROBLEMS: ' + '; '.join(problems)}\n")
    w("## Size")
    w(f"- body lines: {n_body} ({dict(kinds)}), {sum(sizes):,} bytes as Unii counts them")
    w(f"- claude-config lines: {n_claude}")
    if live:
        w(f"- live Unii store now: {live['messages']} messages, {live['main_bytes']:,} bytes main, {live['tree_bytes']:,} bytes tree; "
          f"import would add {len(sizes)} lines = {sum(sizes) / max(live['main_bytes'], 1):.1f}x the current main log")
    w(f"- Unii's own cost guess for compacting this import (port of import_usd): ${usd:.2f}\n")
    w("## Samples (unii-import.jsonl)")
    shown = 0
    for sid, line in unii.body_lines(outs):
        if shown < a.sample:
            w("    " + json.dumps(dict(line, text=line["text"][:140] + ("…" if len(line["text"]) > 140 else "")), ensure_ascii=False))
            shown += 1
    for sid, line in unii.body_lines(outs):
        if line["kind"] == "tool" and shown < a.sample + 3:
            w("    " + json.dumps(dict(line, text=line["text"][:200]), ensure_ascii=False))
            shown += 1
    report = "\n".join(R) + "\n"
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="optmem4hermes2unii", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dry-run", help="convert and validate into --out; writes nothing else")
    d.add_argument("--hermes", required=True, help="Hermes data dir (holds state.db, optmem_memory/)")
    d.add_argument("--out", default="./out")
    d.add_argument("--unii-store", default="~/.opt/users/1", help="live Unii store, read for scale only")
    d.add_argument("--max-tool-chars", type=int, default=4000, help="cut tool results to this many chars (0: no cut)")
    d.add_argument("--max-call-chars", type=int, default=2000, help="cut long string arguments of tool calls (0: no cut)")
    d.add_argument("--keep-gateway", action="store_true", help="keep the 'Gateway message origin' header on user messages")
    d.add_argument("--with-log", action="store_true", help="add optmem LOG.txt entries as a small session")
    d.add_argument("--with-tree", type=int, metavar="LEVEL", help="add optmem TREE/LEVEL summaries as a small session")
    d.add_argument("--sample", type=int, default=6)
    sub.add_parser("apply", help="not implemented: this tool never writes to Unii or Hermes yet")
    a = ap.parse_args(argv)
    if a.cmd == "apply":
        sys.exit("apply is not implemented. Dry run only: nothing is ever written to the Unii store or Hermes.")

    def inside(out):
        out = out.resolve()
        bad = [Path(a.hermes).expanduser().resolve(), Path(a.unii_store).expanduser().resolve()]
        return any(out == b or b in out.parents for b in bad)
    a.hermes_data_is_inside_out = inside
    sys.exit(dry_run(a))


main()
