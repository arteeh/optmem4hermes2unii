"""A UniiChat store (users/<id>/ under $OPT_DIR) and how to put the Hermes history in front of it.

What the Unii JS (v1.0.137, Memory.*) does, and what this relies on:
  * main/<YYYY-MM-DD>.jsonl holds messages {kind, text, ..., i, size, date}; tree/<day>.jsonl holds
    summary lines {l, i, text, size}. Line (l, i) summarizes messages i*2^l .. (i+1)*2^l - 1.
  * main.index / tree.index are 16-byte records (offset, day, length) at position i, resp.
    (2i+1)*2^l - 1. If an index file is missing, Unii rescans all day files (in date order;
    a record goes to the slot named by its own i/l, later lines win) and rebuilds it.
  * view.json holds the two views (lists of (l, i)), the model pick, running agents, and how far
    each stream was indexed ([day, byte]). [0, 0] means "scan from the start".
  * On load, every missing summary whose halves exist is queued. A summary whose source fits in
    512 bytes is copied for free; others cost a compactor call. A message's own `line` field, if
    set, is used as its source (that is how the greeting gets a free summary).
  * The queue of a line whose last message is `imported` is the "import" queue; it starts paused
    and only runs while an import batch (import.json: {a, b, said, spent, why}) is not paused.
    The web UI shows that batch with its cost and a Pause/Resume button (terminal: /import resume).

Plan: Hermes history takes positions 0..N-1, ours move to N.. (i + N). N is padded with empty
notes to a multiple of 2^L (L = the highest level in our tree), so every summary line we already
have stays valid as (l, i + N/2^l): none of ours is rewritten. Only the Hermes lines, and the few
lines above that join the two parts, are new.
"""
import json
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import unii

SLOT = 16
MARK = "(optmem4hermes2unii)"
BOUNDARY = "{source} history import ends here " + MARK + ". Messages 0-{last} are {agent}'s ({source}) history, " \
           "imported in date order; the UniiChat chat goes on from message {n}. Ids written in messages and " \
           "summaries from before this import count from 0 at message {n}: add {n} to them."
FILLER = "(empty: keeps the summary tree aligned)"


def slot(l, i):
    return (2 * i + 1) * 2 ** l - 1


def day_of(name):
    return (datetime.fromisoformat(name[:10]).replace(tzinfo=timezone.utc).timestamp()) // 86400


def local_day(iso):
    """The day file Unii's own Memory.log would write a record into (local date)."""
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t.astimezone().strftime("%Y-%m-%d")


def read_stream(store, stream):
    """Records of main/ or tree/, as Unii's index rebuild sees them: by day file in date order,
    later lines win. Returns {key: (file_day_name, rec)} with key i (main) or (l, i) (tree)."""
    out = {}
    d = Path(store) / stream
    names = sorted((p.name for p in d.glob("*.jsonl")), key=day_of)
    for name in names:
        for line in (d / name).read_text(encoding="utf-8").split("\n"):
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if "i" not in rec:
                continue
            out[rec["i"] if stream == "main" else (rec["l"], rec["i"])] = (name[:10], rec)
    return out


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        if not (self.path / "main").is_dir() or not (self.path / "tree").is_dir():
            raise SystemExit(f"{self.path} is not a Unii store (no main/ and tree/)")
        self.main = read_stream(self.path, "main")
        self.tree = read_stream(self.path, "tree")
        self.count = (max(self.main) + 1) if self.main else 0
        idx = self.path / "main.index"
        self.index_count = idx.stat().st_size // SLOT if idx.exists() else None
        try:
            self.view = json.loads((self.path / "view.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.view = None
        self.batch = (self.path / "import.json").exists()

    def problems(self):
        p = []
        missing = [i for i in range(self.count) if i not in self.main]
        if missing:
            p.append(f"messages missing at {missing[:5]}...")
        if self.count and self.main[self.count - 1][1].get("more"):
            p.append("the last message is an unfinished page/import (more: true)")
        if self.batch:
            p.append("an import is under way (import.json): let it finish first")
        if any(r.get("imported") and r["kind"] == "note" and (MARK in r["text"] or "Hermes (Eris" in r["text"])
               for _, r in self.main.values()):
            p.append("a history was already imported into this store by optmem4hermes2unii")
        bad = [k for k in self.tree if k[1] * 2 ** k[0] + 2 ** k[0] > self.count]
        if bad:
            p.append(f"summary lines past the last message: {bad[:5]}")
        return p

    def top_level(self):
        return max((l for l, _ in self.tree), default=0)


def plan(store, hermes_recs, free_leaves=False, filler_summaries=True, source="Hermes", agent="Eris"):
    """-> dict with all new records and facts. hermes_recs: records from unii.records (no i/size)."""
    M = store.count
    L = store.top_level()
    align = 2 ** L
    H = len(hermes_recs)
    pad = (-(H + 1)) % align           # fillers, then the boundary note, end exactly at N
    N = H + pad + 1
    last_date = hermes_recs[-1]["date"] if hermes_recs else datetime.now(timezone.utc).isoformat()
    first_ours = store.main[0][1]["date"] if M else None

    new = []
    for k, r in enumerate(hermes_recs):
        rec = dict(r)
        rec["text"] = unii.well_formed(rec["text"])
        new.append(rec)
    for k in range(pad):
        new.append(dict(kind="note", text=FILLER, imported=True, date=last_date))
    new.append(dict(kind="note", text=BOUNDARY.format(last=N - 1, n=N, source=source, agent=agent), imported=True, date=last_date))

    main = []                          # (day name, rec) in position order
    free_lines = 0
    for i, r in enumerate(new):
        # Memory.root: {...msg, text, i, size, date}
        rec = {k: v for k, v in r.items() if k not in ("date",)}
        rec["i"] = i
        rec["size"] = unii.bytes_(unii.show(rec))
        rec["date"] = r["date"]
        if free_leaves and rec["size"] > unii.NODE:
            line = unii.flat(unii.show(rec))
            rec_line = unii.clip512(line)
            if unii.bytes_(rec_line) > unii.NODE - 3:
                rec_line = rec_line.encode("utf-8")[:unii.NODE - 3].decode("utf-8", "ignore") + "…"
            rec = {**{k: v for k, v in rec.items() if k not in ("i", "size", "date")}, "line": rec_line,
                   "i": rec["i"], "size": rec["size"], "date": rec["date"]}
            free_lines += 1
        main.append((local_day(rec["date"]), rec))
    for i in range(M):
        name, r = store.main[i]
        rec = dict(r)
        rec["i"] = i + N
        main.append((name, rec))

    tree = []                          # (day name, rec)
    for (l, i), (name, r) in sorted(store.tree.items(), key=lambda kv: slot(*kv[0])):
        rec = dict(r)
        rec["i"] = i + N // 2 ** l
        tree.append((name, rec))
    # lines over fillers only: written here, so they cost nothing and read as one line
    fa, fz = H, H + pad                # filler positions fa .. fz-1
    tday = local_day(last_date)
    for l in range(0, 31 if filler_summaries else 0):
        n = 2 ** l
        if n > pad:
            break
        for i in range(-(-fa // n), fz // n):
            a, z = i * n, i * n + n - 1
            text = f"note: {n} empty alignment notes, {a}-{z}" if n > 1 else "note: " + FILLER
            tree.append((tday, dict(l=l, i=i, text=text, size=unii.bytes_(text))))

    view = None
    if store.view is not None:
        def shift(parts):
            return [[l, i + N // 2 ** l] for l, i in parts]
        v = store.view
        view = dict(parts=shift(v.get("parts", [])), batch=v.get("batch", False),
                    seen=dict(parts=shift(v.get("seen", {}).get("parts", [])), batch=v.get("seen", {}).get("batch", False)),
                    pick=v.get("pick"), work=v.get("work", []), main=[0, 0], tree=[0, 0])
    said = sum(1 for r in hermes_recs if r["kind"] != "note" and not r.get("more"))
    late = [r for r in hermes_recs if first_ours and r["date"] > first_ours]
    leaf_sizes = [min(rec["size"], unii.bytes_(rec["line"])) if "line" in rec else
                  (rec["size"] if rec["size"] > unii.NODE else rec["size"]) for _, rec in main[:N]]
    return dict(M=M, L=L, align=align, H=H, pad=pad, N=N, main=main, tree=tree, view=view,
                batch=dict(a=0, b=N, said=said, spent=0), late=late, first_ours=first_ours,
                free_lines=free_lines, leaf_sizes=leaf_sizes)


def jline(rec):
    return unii.jdump(rec)


def write(p, dest, start=False):
    """Write the planned store files into dest (an empty dir, or a store being replaced)."""
    dest = Path(dest)
    for stream in ("main", "tree"):
        d = dest / stream
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*.jsonl"):
            f.unlink()
        by_day = {}
        for name, rec in p[stream]:
            by_day.setdefault(name, []).append(rec)
        for name, recs in by_day.items():
            tmp = d / (".o4h2u-" + name + ".jsonl.new")
            tmp.write_text("".join(jline(r) + "\n" for r in recs), encoding="utf-8")
            os.replace(tmp, d / (name + ".jsonl"))
    for f in ("main.index", "tree.index"):
        (dest / f).unlink(missing_ok=True)
    if p["view"] is not None:
        (dest / "view.json").write_text(json.dumps(p["view"], separators=(",", ":")), encoding="utf-8")
    else:
        (dest / "view.json").unlink(missing_ok=True)
    (dest / "import.json").write_text(json.dumps(dict(p["batch"], why=None if start else "paused"), separators=(",", ":")),
                                      encoding="utf-8")


def verify(store_path, before=None, p=None):
    """Re-read a written store the way Unii's index rebuild would, and check it. -> (problems, facts)"""
    s = Store(store_path)
    probs = []
    count = s.count
    missing = [i for i in range(count) if i not in s.main]
    if missing:
        probs.append(f"holes in message positions: {missing[:5]}")
    for (l, i), (_, r) in s.tree.items():
        if (i + 1) * 2 ** l > count:
            probs.append(f"summary ({l},{i}) past the last message")
            break
        if unii.bytes_(r["text"]) > unii.NODE * 4:
            probs.append(f"summary ({l},{i}) too long")
    if before is not None and p is not None:
        N = p["N"]
        for i in range(before.count):
            a = dict(before.main[i][1])
            b = dict(s.main[i + N][1])
            if b.pop("i") != i + N or a.pop("i") != i or a != b:
                probs.append(f"our message {i} differs after the move (now {i + N})")
                break
        for (l, i), (_, r) in before.tree.items():
            got = s.tree.get((l, i + N // 2 ** l))
            if got is None or got[1]["text"] != r["text"]:
                probs.append(f"our summary ({l},{i}) lost")
                break
        dates = [s.main[i][1]["date"] for i in range(p["H"])]
        back = sum(1 for x, y in zip(dates, dates[1:]) if y < x)
        if back:
            probs.append(f"{back} Hermes dates run backwards")
    # what Unii will queue on load: roots -> todo (missing lines whose halves exist, recursively)
    have = set(s.tree)

    def built(l, i):
        return (l, i) in have

    def todo(l, i, out):
        if built(l, i):
            return
        if l == 0 or (built(l - 1, 2 * i) and built(l - 1, 2 * i + 1)):
            out.append((l, i))
        if l > 0:
            todo(l - 1, 2 * i, out)
            todo(l - 1, 2 * i + 1, out)
    roots, idn = [], 0
    for l in range(30, -1, -1):
        if idn + 2 ** l <= count:
            roots.append((l, idn // 2 ** l))
            idn += 2 ** l
    missing_lines = 0
    for l, i in roots:
        # count every missing line under each root (all get built eventually)
        stack = [(l, i)]
        while stack:
            a, b = stack.pop()
            if not built(a, b):
                missing_lines += 1
            if a > 0:
                stack += [(a - 1, 2 * b), (a - 1, 2 * b + 1)]
    queued = []
    for l, i in roots:
        todo(l, i, queued)

    def side(l, i):
        last = (i + 1) * 2 ** l - 1
        return 1 if s.main[last][1].get("imported") else 0
    sides = Counter(side(l, i) for l, i in queued)
    free_now = sum(1 for l, i in queued if l == 0 and unii.bytes_(s.main[i][1].get("line") or unii.show(s.main[i][1])) <= unii.NODE)
    facts = dict(messages=count, summaries=len(s.tree), missing_lines=missing_lines, queued_at_load=len(queued),
                 queued_import_side=sides.get(1, 0), queued_compactor_side=sides.get(0, 0), queued_free_leaves=free_now,
                 top_levels=Counter(l for l, _ in s.tree).most_common()[:0] or max((l for l, _ in s.tree), default=0),
                 index_files=[f for f in ("main.index", "tree.index") if (Path(store_path) / f).exists()],
                 day_files=dict(main=sorted(x.name for x in (Path(store_path) / "main").glob("*.jsonl")),
                                tree=sorted(x.name for x in (Path(store_path) / "tree").glob("*.jsonl"))))
    return probs, facts


def copy_store(src, dst):
    shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("*.sock"))


def holders(path):
    """PIDs of processes holding a file open inside path (Linux /proc)."""
    path = str(Path(path).resolve())
    out = set()
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    if os.readlink(f"/proc/{pid}/fd/{fd}").startswith(path + "/"):
                        out.add(int(pid))
                except OSError:
                    pass
        except OSError:
            pass
    return sorted(out)
