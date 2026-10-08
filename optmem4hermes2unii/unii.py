"""Hermes messages -> what UniiChat's own importer (v1.0.137) takes.

Everything here mirrors the importer found in the Unii JS (read-only study):
  * an import is a list of sessions; each starts with a note
    {kind:"note", text:"<app> session in <where>, from <time> (imported)", date}
    followed by {kind, text, date, more} lines, kind in user|unii|tool|echo;
  * a tool call is  "<name> <compact JSON args>", its result an echo;
  * texts are paged at 30000 chars (more:true on all but the last page);
  * Unii shows imported unii/tool messages as "ai" (another AI's).
We write that body (unii-import.jsonl) and also Claude-Code-style session files, which
`/import claude` already knows how to read, then port the importer's parser to prove
both say the same thing.
"""
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

CAP = 30000
SKIP = re.compile(r"\A(<command-|<local-command|\[Request interrupted)")
NODE, LINE = 512, 480                      # importer's cost model constants
PRICE = dict(base=187, byte=735, depth=628)  # micro-dollars


@dataclass
class Out:
    kind: str
    text: str
    date: str
    hid: int = 0
    tool: str = ""
    args: object = None


@dataclass
class OutSession:
    id: str
    label: str      # what we call it in the note
    where: str      # the "session in <where>" part
    start: str
    msgs: list = field(default_factory=list)


def cut(text, n):
    """Like Unii's own clip: head + marker + tail."""
    if n <= 0 or len(text) <= n:
        return text
    keep = n // 2
    gone = len(text) - 2 * keep
    return text[:keep] + f"\n[... {gone} characters cut ...]\n" + text[-keep:]


def cut_args(obj, n):
    if isinstance(obj, str):
        return cut(obj, n)
    if isinstance(obj, list):
        return [cut_args(x, n) for x in obj]
    if isinstance(obj, dict):
        return {k: cut_args(v, n) for k, v in obj.items()}
    return obj


def jdump(x):
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def tool_text(name, args):
    return name + " " + jdump(args)


def pages(text):
    """Port of clip_pages: split at the last newline within CAP, else at CAP."""
    out, at = [], 0
    while True:
        if len(text) - at <= CAP:
            end = len(text)
        else:
            nl = text.rfind("\n", at, at + CAP)
            end = nl + 1 if nl >= at else at + CAP
        out.append(text[at:end])
        at = end
        if at >= len(text):
            return out


def msgs_of(kind, text, date):
    if text.strip() == "":
        return []
    ps = pages(text)
    return [dict(kind=kind, text=p, date=date, more=k < len(ps) - 1) for k, p in enumerate(ps)]


def note_for(s):
    return dict(kind="note", text=f"{s.label} session in {s.where}, from {s.start[:16].replace('T', ' ')} (imported)",
                date=s.start, more=False)


def build(sessions, opt):
    """hermes.Session list -> OutSession list (clipped, escaped)."""
    outs, esc = [], 0
    for s in sessions:
        os_ = OutSession(s.id, f"Hermes (Eris, {s.source})", f"hermes/{s.source}/{s.id}", s.msgs[0].date)
        for m in s.msgs:
            if m.kind == "tool":
                o = Out("tool", tool_text(m.tool, cut_args(m.args, opt["max_call_chars"])), m.date, m.hid, m.tool,
                        cut_args(m.args, opt["max_call_chars"]))
            else:
                t = cut(m.text, opt["max_tool_chars"]) if m.kind == "echo" else m.text
                if m.kind in ("user", "unii") and SKIP.match(t):
                    t, esc = " " + t, esc + 1   # the importer would drop such a text block
                o = Out(m.kind, t, m.date, m.hid, m.tool)
            if o.text.strip():
                os_.msgs.append(o)
        outs.append(os_)
    return outs, esc


def body_lines(outs):
    """The import body: per session a note then its (paged) messages."""
    for s in outs:
        yield s.id, note_for(s)
        for o in s.msgs:
            for line in msgs_of(o.kind, o.text, o.date):
                yield s.id, line


def write_body(outs, path):
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for _, line in body_lines(outs):
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            n += 1
    return n


def claude_lines(s):
    """Claude Code session lines for one session (what line_claude reads)."""
    base = dict(cwd=s.where, sessionId=s.id, isSidechain=False)
    for i, o in enumerate(s.msgs):
        if o.kind == "user":
            yield dict(base, type="user", timestamp=o.date, message=dict(role="user", content=[dict(type="text", text=o.text)]))
        elif o.kind == "unii":
            yield dict(base, type="assistant", timestamp=o.date, message=dict(role="assistant", content=[dict(type="text", text=o.text)]))
        elif o.kind == "tool":
            yield dict(base, type="assistant", timestamp=o.date, message=dict(role="assistant", content=[
                dict(type="tool_use", id=f"h{o.hid}-{i}", name=o.tool, input=o.args)]))
        else:
            yield dict(base, type="user", timestamp=o.date, message=dict(role="user", content=[
                dict(type="tool_result", tool_use_id=f"h{o.hid}-{i}", content=o.text)]))


def write_claude(outs, root):
    root = Path(root)
    n = 0
    for s in outs:
        d = root / "projects" / "-hermes-eris"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{s.id}.jsonl", "w", encoding="utf-8") as f:
            for line in claude_lines(s):
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
                n += 1
    return n


def read_claude(root):
    """Port of line_claude/blocks_msgs: what Unii's importer would make of the files."""
    got = {}
    for p in sorted(Path(root).glob("projects/*/*.jsonl")):
        msgs, date = [], ""
        for line in p.read_text(encoding="utf-8").splitlines():
            j = json.loads(line)
            date = j["timestamp"]
            c = j["message"]["content"]
            kind = "user" if j["type"] == "user" else "unii"
            say = [b for b in c if b["type"] == "text" and not SKIP.match(b["text"])]
            msgs += msgs_of(kind, "".join(b["text"] for b in say), date)
            for b in c:
                if b["type"] == "tool_use":
                    msgs += msgs_of("tool", b["name"] + " " + jdump(b["input"]), date)
                elif b["type"] == "tool_result":
                    msgs += msgs_of("echo", b["content"], date)
        got[p.stem] = msgs
    return got


# ---- the importer's cost guess (import_guess / node_usd), to say what compaction will cost
def _usd(node):
    if node[0] <= NODE:
        return 0
    return round(PRICE["base"] + PRICE["byte"] * math.log2(node[0] / NODE) + PRICE["depth"] * (node[1] + 1))


def guess_usd(a, sizes):
    nodes = [(b, 0) for b in sizes]
    total, i, l = 0, a, 0
    while nodes:
        total += sum(_usd(n) for n in nodes)

        def line(k, nodes=nodes, i=i, l=l):
            if k < 0:
                return (LINE, l)
            n = nodes[k]
            return (LINE, n[1] + 1) if n[0] > NODE else (n[0], 0)
        frm = i // 2
        cnt = (i + len(nodes)) // 2 - frm
        nodes = [(line(2 * (frm + j) - i)[0] + 1 + line(2 * (frm + j) + 1 - i)[0],
                  max(line(2 * (frm + j) - i)[1], line(2 * (frm + j) + 1 - i)[1])) for j in range(cnt)]
        i //= 2
        l += 1
    return total / 1e6


def size(kind, text):
    return len((kind + ": " + text).encode("utf-8"))
