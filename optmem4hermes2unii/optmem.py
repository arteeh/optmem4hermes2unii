"""Optional extras from rarf's optmem plugin: LOG.txt entries and TREE/<level> summaries.

Both are already implied by the raw messages (the agent wrote LOG entries through
optmem_note calls), so they are off by default. They become small extra sessions
of the agent's own notes ("unii" kind, shown by Unii as "ai").
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import hermes
from .unii import OutSession, Out

ENTRY = re.compile(r"^#(\d+) (\d{4}-\d\d-\d\d) ?(.*)$", re.S)


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def note_times(data_dir):
    """entry number -> unix time of the optmem_note tool result that created it."""
    con = hermes.open_ro(data_dir)
    out = {}
    for content, ts in con.execute("select content, timestamp from messages where role='tool' and content like '%saved_as%' order by id"):
        try:
            j = json.loads(content)
        except ValueError:
            continue
        m = re.fullmatch(r"#(\d+)", str(j.get("saved_as", "")))
        if m and j.get("status") == "added":
            out.setdefault(int(m.group(1)), ts)
    con.close()
    return out


def log_session(data_dir, source="Hermes", agent="agent"):
    p = Path(data_dir).expanduser() / "optmem_memory" / "LOG.txt"
    if not p.exists():
        return None, {}
    times = note_times(data_dir)
    msgs, prev, fallback = [], 0.0, 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = ENTRY.match(line.strip())
        if not m:
            continue
        n, day, text = int(m.group(1)), m.group(2), m.group(3).strip()
        if text.startswith(day):
            text = text[len(day):].strip()
        if n in times:
            ts = times[n]
        else:
            fallback += 1
            ts = datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()
        ts = max(ts, prev)
        prev = ts
        msgs.append(Out("unii", f"optmem #{n} ({day}): {text}", _iso(ts), hid=n))
    if not msgs:
        return None, {}
    s = OutSession("optmem-log", f"{source} ({agent}) optmem", f"{source.lower()}/optmem/LOG", msgs[0].date, msgs)
    return s, dict(entries=len(msgs), dated_from_messages=len(msgs) - fallback, dated_by_day_only=fallback)


def tree_session(data_dir, level, source="Hermes", agent="agent"):
    p = Path(data_dir).expanduser() / "optmem_memory" / "TREE" / str(level)
    if not p.exists():
        raise SystemExit(f"no TREE level {level} in {p.parent}")
    ts = p.stat().st_mtime
    msgs = [Out("unii", f"optmem summary (level {level}, #{k}): {line.strip()}", _iso(ts), hid=k)
            for k, line in enumerate(p.read_text(encoding="utf-8").splitlines()) if line.strip()]
    return OutSession(f"optmem-tree-{level}", f"{source} ({agent}) optmem", f"{source.lower()}/optmem/TREE/{level}", msgs[0].date, msgs), dict(
        level=level, nodes=len(msgs), dated="file mtime (the tree has no per-node dates)")
