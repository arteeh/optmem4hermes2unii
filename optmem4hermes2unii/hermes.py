"""Read Hermes' state.db (read-only) and rebuild the real conversation, in order.

Hermes rewrites history at every context compaction: the old rows stay, and a
"demoted" copy of the earlier turns (tool output replaced by a one-line stub,
fresh timestamps on assistant/tool rows) is inserted after them. So ids are in
true conversation order, but the same turn can occur several times. The first
occurrence is the original; later copies are dropped.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

GATEWAY = re.compile(
    r"\A(?:\[OUT-OF-BAND USER MESSAGE[^\n]*\]\n)?Gateway message origin \(JSON data[^\n]*\n\{[^\n]*\}\n"
    r"Do not guess a reply destination[^\n]*\n\n?", re.S)
TRIGGER = re.compile(r"\A\[Triggering message id: `[^`]*` [^\]\n]*\]\n\n?")
DEMOTED = re.compile(r"\A(\[[\w.-]+\] ran `|\[[\w.-]+\] \(\d[\d,]* chars result\)|\[[\w.-]+ output demoted at compaction)")


@dataclass
class Msg:
    kind: str            # user | unii | tool | echo
    text: str
    ts: float            # unix seconds (Hermes' own timestamp)
    sid: str
    hid: int             # Hermes messages.id
    demoted: bool = False  # only a compaction stub of the original survived
    tool: str = ""       # tool name (tool/echo)
    args: object = None  # parsed tool arguments (tool)
    date: str = ""       # filled later: final ISO date


@dataclass
class Session:
    id: str
    source: str
    model: str
    title: str
    started: float
    ended: float
    parent: str
    cwd: str
    msgs: list = field(default_factory=list)


def open_ro(data_dir):
    db = Path(data_dir).expanduser() / "state.db"
    if not db.exists():
        raise SystemExit(f"no state.db in {db.parent}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def load(data_dir, keep_gateway=False):
    """Return (sessions, stats). Sessions sorted by start, messages by Hermes id."""
    con = open_ro(data_dir)
    st = dict(rows=0, session_meta=0, summaries=0, hidden_empty=0, dup_user=0, dup_call=0,
              dup_result=0, dup_text=0, failed_turn=0, steer=0, notification=0, gateway_stripped=0,
              demoted_only=0, tool_calls=0, empty=0, skip_escaped=0)
    sessions = {}
    for r in con.execute("select id,source,model,title,started_at,ended_at,parent_session_id,cwd from sessions"):
        sessions[r[0]] = Session(r[0], r[1], r[2] or "", r[3] or "", r[4], r[5] or r[4], r[6] or "", r[7] or "")
    seen_call, seen_res, seen_user, seen_text = set(), set(), set(), set()
    prev_copy = False  # was the previous row a dropped compaction copy?
    q = ("select id,session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,compacted,"
         "_compressed_summary,display_kind from messages order by id")
    for (hid, sid, role, content, tcid, tcalls, tname, ts, compacted, summ, dk) in con.execute(q):
        st["rows"] += 1
        was_copy, prev_copy = prev_copy, False
        s = sessions[sid]
        if role == "session_meta":
            st["session_meta"] += 1
            continue
        if summ:  # Hermes' own compaction handoff summary: not a message
            st["summaries"] += 1
            continue
        content = content or ""
        if role == "user":
            if not keep_gateway:
                new = TRIGGER.sub("", GATEWAY.sub("", content, count=1), count=1)
                if new != content and new.strip():
                    content = new
                    st["gateway_stripped"] += 1
            key = (sid, round(ts, 3), content)
            if key in seen_user:
                st["dup_user"] += 1
                prev_copy = True
                continue
            seen_user.add(key)
            kind = "user"
            if dk == "internal_notification":
                kind = "echo"
                st["notification"] += 1
            if dk == "steer":
                st["steer"] += 1
            s.msgs.append(Msg(kind, content, ts, sid, hid, tool="notification" if kind == "echo" else ""))
        elif role == "assistant":
            calls = json.loads(tcalls) if tcalls else []
            if dk == "failed_turn":
                st["failed_turn"] += 1  # kept as plain text: it is what the user saw
            if calls:
                ids = tuple(c.get("id") or c.get("call_id") for c in calls)
                if all(i in seen_call for i in ids):
                    st["dup_call"] += 1
                    prev_copy = True
                    continue
                seen_call.update(ids)
                if content.strip():
                    s.msgs.append(Msg("unii", content, ts, sid, hid))
                for c in calls:
                    fn = c.get("function", {})
                    raw = fn.get("arguments", "")
                    try:
                        args = json.loads(raw) if raw else {}
                    except ValueError:
                        args = {"_raw": raw}
                    s.msgs.append(Msg("tool", "", ts, sid, hid, tool=fn.get("name", "tool"), args=args))
                    st["tool_calls"] += 1
            else:
                if not content.strip():
                    st["hidden_empty"] += 1
                    continue
                if (sid, content) in seen_text and (compacted or was_copy):
                    st["dup_text"] += 1
                    prev_copy = True
                    continue
                seen_text.add((sid, content))
                s.msgs.append(Msg("unii", content, ts, sid, hid))
        elif role == "tool":
            if tcid in seen_res:
                st["dup_result"] += 1
                prev_copy = True
                continue
            seen_res.add(tcid)
            dem = bool(DEMOTED.match(content))
            st["demoted_only"] += dem
            s.msgs.append(Msg("echo", content, ts, sid, hid, demoted=dem, tool=tname or ""))
    out = [s for s in sessions.values() if s.msgs]
    out.sort(key=lambda s: (s.started, s.id))
    con.close()
    return out, st


def assign_dates(sessions, stats):
    """Set Msg.date (ISO, ms, Z) = Hermes timestamp, never going backwards inside a session.
    Hermes stamps a message when it arrives, but appends it in the order it consumed it, so
    ids can run slightly ahead of timestamps; ids (the order) win, dates are clamped."""
    from datetime import datetime, timezone
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stats.update(clamped=0, max_clamp_s=0.0, ties=0)
    for s in sessions:
        prev = s.started
        for k, x in enumerate(s.msgs):
            if x.ts < prev:
                stats["clamped"] += 1
                stats["max_clamp_s"] = max(stats["max_clamp_s"], round(prev - x.ts, 1))
                x.ts = prev
            x.date = iso(x.ts)
            if k and x.date == s.msgs[k - 1].date:
                stats["ties"] += 1
            prev = x.ts
