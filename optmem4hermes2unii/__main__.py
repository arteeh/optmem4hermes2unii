"""optmem4hermes2unii: move Hermes (+ rarf's optmem plugin) memory into a UniiChat store, in date order.

  dry-run  copy the store to --out, put the Hermes history in the copy, verify it, report
  apply    the same on the store itself (unii serve must be stopped), after a backup
  verify   check a store the way Unii's index rebuild reads it
"""
import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import hermes, optmem, store, unii


def hermes_records(a):
    sessions, st = hermes.load(a.hermes, keep_gateway=a.keep_gateway)
    hermes.assign_dates(sessions, st)
    outs = unii.build(sessions, dict(max_tool_chars=a.max_tool_chars, max_call_chars=a.max_call_chars))
    outs.sort(key=lambda s: (s.start, s.id))
    extras = {}
    if a.with_log:
        s, info = optmem.log_session(a.hermes)
        if s:
            s.extra = True
            outs.append(s)
            extras["LOG.txt"] = info
    if a.with_tree:
        s, info = optmem.tree_session(a.hermes, a.with_tree)
        s.extra = True
        outs.append(s)
        extras[f"TREE/{a.with_tree}"] = info
    recs = [r for _, r in unii.records(outs)]
    return outs, recs, st, extras


def run(a, dest, live):
    """Plan against the store at a.store, write into dest (a copy, or the store itself). -> report text, problems"""
    before = store.Store(a.store)
    bad = before.problems()
    if bad:
        raise SystemExit("refusing: " + "; ".join(bad))
    outs, recs, st, extras = hermes_records(a)
    first_ours = before.main[0][1]["date"] if before.count else None
    late = [r for r in recs if first_ours and r["date"] > first_ours]
    if late and a.late == "drop":
        recs = [r for r in recs if not (first_ours and r["date"] > first_ours)]
    p = store.plan(before, recs, free_leaves=a.free_leaves)
    store.write(p, dest, start=a.start)
    probs, facts = store.verify(dest, before, p)

    # cost: Unii's own guess (import_guess) over the imported block, as written and the alternatives
    sizes = p["leaf_sizes"]
    usd = unii.guess_usd(0, sizes)
    usd_free = unii.guess_usd(0, [min(x, unii.NODE) for x in sizes])
    leaves_paid = sum(1 for x in sizes if x > unii.NODE)
    joins = facts["missing_lines"] - sum(len(lv) for lv in unii.guess(0, sizes))

    R = []
    w = R.append
    w(f"# optmem4hermes2unii: {'APPLIED to ' + str(a.store) if live else 'dry run'}\n")
    w(f"Hermes: {a.hermes}/state.db (read-only). Store read: {a.store}. Written: {dest}"
      + ("" if live else " (a copy; the store itself was not touched)") + "\n")
    w("## Hermes rows -> messages")
    w(f"- rows read: {st['rows']}; kept as {sum(len(s.msgs) for s in outs if not s.extra)} messages "
      f"({st['tool_calls']} tool calls; one Hermes row can hold text + several calls)")
    w(f"- dropped as Hermes compaction copies: {st['dup_user']} user, {st['dup_call']} tool-call, {st['dup_result']} tool-result, "
      f"{st['dup_text']} assistant-text rows (the originals are kept)")
    w(f"- dropped other: {st['session_meta']} session_meta, {st['summaries']} Hermes compaction recaps, {st['hidden_empty']} empty rows")
    w(f"- cut: tool results to {a.max_tool_chars} chars, tool-call string args to {a.max_call_chars} (0 = no cut)")
    w(f"- background-process notices kept as echo: {st['notification']}; gateway headers stripped: {st['gateway_stripped']}")
    w(f"- extras (optmem): {extras or 'none'}\n")
    w("## Sessions")
    w("| session | messages | kinds | first | last |\n|---|---|---|---|---|")
    for s in outs:
        c = Counter(o.kind for o in s.msgs)
        w(f"| {s.id} | {len(s.msgs)} | {dict(c)} | {s.msgs[0].date} | {s.msgs[-1].date} |")
    w("")
    w("## Placement (date order, no summary of ours rewritten)")
    w(f"- Hermes records (notes + pages): {p['H']} at positions 0-{p['H'] - 1}; {p['pad']} empty alignment notes; "
      f"boundary note at {p['N'] - 1}")
    w(f"- our {p['M']} messages move from 0-{p['M'] - 1} to {p['N']}-{p['N'] + p['M'] - 1}; "
      f"N = {p['N']} is a multiple of 2^{p['L']} = {p['align']} (our highest summary level), so all "
      f"{len(before.tree)} of our summary lines are kept, renumbered, unchanged")
    w(f"- Hermes messages dated after our first message ({first_ours}): {len(late)}"
      + (f" -> {a.late}" + (" (they sit before ours, slightly out of date order)" if a.late == "keep" else "") if late else ""))
    if late:
        w(f"  first: {late[0]['date']} {late[0]['kind']}: {late[0]['text'][:80]!r}")
    w(f"- day files: {facts['day_files']}")
    w(f"- index files removed (Unii rebuilds them on load): {not facts['index_files']}; view.json: "
      f"{'kept (pick, agents), parts renumbered, scan from start' if p['view'] else 'none'}")
    w(f"- import.json: {dict(p['batch'], why=None if a.start else 'paused')} -> the web UI shows it as an import "
      f"with its cost; {'it starts when unii serve starts' if a.start else 'press Resume (or /import resume) to start it'}\n")
    w("## Cost (summaries Unii will make)")
    w(f"- after load: {facts['messages']} messages, {facts['summaries']} summary lines present, "
      f"{facts['missing_lines']} to make ({facts['queued_at_load']} queued at once, the rest as their halves finish)")
    w(f"- leaves (one per Hermes record): {p['N'] - leaves_paid} free (<= 512 bytes), {leaves_paid} need the compactor"
      + (f"; {p['free_lines']} long ones got a cut-off `line` instead (--free-leaves)" if a.free_leaves else ""))
    w(f"- Unii's own estimate for the import block (port of import_guess, compactor default claude-api haiku-5.5): ${usd:.2f}")
    if not a.free_leaves:
        w(f"  - with --free-leaves (long Hermes messages get their first 512 bytes as their line, no call): ${usd_free:.2f}")
    w(f"- lines joining Hermes and our part (normal compactor queue, after the import): about {max(joins, 0)}")
    w(f"- none of our {len(before.tree)} existing summaries is rewritten\n")
    w("## Checks")
    w("- " + ("ALL OK: every position filled, our messages identical except i, all our summaries at their new place, "
              "Hermes dates in order" if not probs else "PROBLEMS: " + "; ".join(probs)))
    w("\n## First records")
    for name, rec in p["main"][:4] + p["main"][p["N"] - 2:p["N"] + 1]:
        w("    " + unii.jdump({k: (v[:120] + "…" if isinstance(v, str) and len(v) > 120 else v) for k, v in rec.items()}))
    return "\n".join(R) + "\n", probs


def dry_run(a):
    out = Path(a.out).expanduser().resolve()
    src = Path(a.store).expanduser().resolve()
    if out == src or src in out.parents or out in src.parents:
        raise SystemExit("refusing: --out may not be the store, inside it or around it")
    copy = out / "store"
    if out.exists():
        if any(out.iterdir()) and not (out / "report.md").exists():
            raise SystemExit(f"refusing: {out} exists, is not empty and is not an earlier dry-run output")
        import shutil
        shutil.rmtree(out)
    out.mkdir(parents=True)
    store.copy_store(src, copy)
    report, probs = run(argparse.Namespace(**{**vars(a), "store": str(copy)}), copy, live=False)
    report = report.replace(str(copy), f"{copy} (copy of {src})", 1)
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 1 if probs else 0


def apply(a):
    src = Path(a.store).expanduser().resolve()
    busy = [p for p in store.holders(src) if p != os.getpid()]
    if busy:
        raise SystemExit(f"refusing: processes {busy} have files open in {src}. Stop unii serve first "
                         "(systemctl --user stop unii-serve.service unii.service).")
    opt = Path(os.environ.get("OPT_DIR", "~/.opt")).expanduser().resolve()
    if (src == opt or opt in src.parents) and not a.live:
        raise SystemExit(f"refusing: {src} is inside the live Unii data dir {opt}; add --live to mean it")
    bad = store.Store(src).problems()
    if bad:
        raise SystemExit("refusing: " + "; ".join(bad))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = Path(a.backup or f"~/.local/share/optmem4hermes2unii/backup-{src.name}-{stamp}").expanduser().resolve()
    if backup == src or src in backup.parents:
        raise SystemExit("refusing: --backup may not be inside the store")
    store.copy_store(src, backup)
    print(f"backup: {backup}")
    try:
        report, probs = run(a, src, live=True)
    except SystemExit as e:
        if str(e).startswith("refusing"):   # refused before writing anything
            raise
        restore(backup, src)
        print(f"failed: the store was restored from {backup}", file=sys.stderr)
        raise
    except BaseException:
        restore(backup, src)
        print(f"failed: the store was restored from {backup}", file=sys.stderr)
        raise
    if probs:
        restore(backup, src)
        report += f"\nRESTORED from {backup} because of the problems above.\n"
    rep = backup.parent / f"report-{src.name}-{stamp}.md"
    rep.write_text(report, encoding="utf-8")
    print(report)
    print(f"report: {rep}")
    return 1 if probs else 0


def restore(backup, src):
    import shutil
    for x in list(src.iterdir()):
        if x.is_dir() and not x.is_symlink():
            shutil.rmtree(x)
        else:
            x.unlink()
    for x in backup.iterdir():
        if x.is_dir() and not x.is_symlink():
            shutil.copytree(x, src / x.name, symlinks=True)
        else:
            shutil.copy2(x, src / x.name, follow_symlinks=False)


def verify(a):
    probs, facts = store.verify(a.store)
    print(json.dumps(facts, indent=1))
    print("OK" if not probs else "PROBLEMS: " + "; ".join(probs))
    return 1 if probs else 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="optmem4hermes2unii", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(x):
        x.add_argument("--hermes", required=True, help="Hermes data dir (holds state.db, optmem_memory/)")
        x.add_argument("--store", required=True, help="Unii store: $OPT_DIR/users/<id> (e.g. ~/.opt/users/1)")
        x.add_argument("--max-tool-chars", type=int, default=4000, help="cut tool results to this many chars (0: no cut)")
        x.add_argument("--max-call-chars", type=int, default=2000, help="cut long string arguments of tool calls (0: no cut)")
        x.add_argument("--keep-gateway", action="store_true", help="keep the 'Gateway message origin' header on user messages")
        x.add_argument("--with-log", action="store_true", help="add optmem LOG.txt entries as a small extra session")
        x.add_argument("--with-tree", type=int, metavar="LEVEL", help="add optmem TREE/LEVEL summaries as a small extra session")
        x.add_argument("--late", choices=("keep", "drop"), default="keep",
                       help="Hermes messages dated after the first Unii message: keep them (before ours) or drop them")
        x.add_argument("--free-leaves", action="store_true",
                       help="give long Hermes messages their first 512 bytes as summary line (no compactor call for them)")
        x.add_argument("--start", action="store_true", help="let the import's summaries start at once (default: paused)")
    d = sub.add_parser("dry-run", help="do it all on a copy of the store in --out")
    common(d)
    d.add_argument("--out", default="./out")
    p = sub.add_parser("apply", help="do it on the store (unii serve stopped); backup first, restore on any problem")
    common(p)
    p.add_argument("--backup", help="backup dir (default ~/.local/share/optmem4hermes2unii/backup-<id>-<time>)")
    p.add_argument("--live", action="store_true", help="allow a store inside $OPT_DIR (the real Unii data)")
    v = sub.add_parser("verify", help="check a store as Unii's index rebuild reads it")
    v.add_argument("--store", required=True)
    a = ap.parse_args(argv)
    sys.exit(dict(**{"dry-run": dry_run, "apply": apply, "verify": verify})[a.cmd](a))


main()
