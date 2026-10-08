"""UniiChat (v1.0.137) message rules, ported from its JS (read-only study).

  * a Hermes session becomes a note {kind:"note", text:"<app> session in <where>, from <time> (imported)"}
    followed by its messages, kind in user|unii|tool|echo, each with Hermes' own `date`;
  * a tool call is  "<name> <compact JSON args>", its result an echo;
  * texts are paged at 30000 UTF-16 units (clip_pages; `more: true` on all but the last page);
  * messages carry `imported: true`, so Unii shows imported unii/tool messages as "ai" (another AI's)
    and its compactor bills their summaries as an "import" (the pausable queue);
  * a message's `size` is the UTF-8 length of what Unii shows: "<who>: <text>".
"""
import array
import json
import math
import re
from dataclasses import dataclass, field

CAP = 30000                                # clip_pages page size, in UTF-16 code units
NODE, LINE = 512, 480                      # summary line limit; importer's cost model line guess
PRICE = dict(base=187, byte=735, depth=628)  # importer's cost model, micro-dollars per node


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
    extra: bool = False   # optmem extras go after the real sessions


def cut(text, n):
    """Head + marker + tail, like Unii's own clip."""
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
    """JSON.stringify: no spaces, raw non-ASCII."""
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def well_formed(text):
    """String.prototype.toWellFormed: lone surrogates -> U+FFFD."""
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def pages(text):
    """Port of clip_pages/clip_end (JS string indexes are UTF-16 code units)."""
    u = array.array("H")
    u.frombytes(text.encode("utf-16-le", "surrogatepass"))
    n, out, at = len(u), [], 0

    def s(a, b):
        return u[a:b].tobytes().decode("utf-16-le", "surrogatepass")
    while True:
        if n - at <= CAP:
            end = n
        else:
            nl = -1
            for k in range(at + CAP - 1, at - 1, -1):
                if u[k] == 10:
                    nl = k
                    break
            low = (u[at + CAP] >> 10) == 55
            end = nl + 1 if nl >= at else at + CAP - (1 if low else 0)
        out.append(s(at, end))
        at = end
        if at >= n:
            return out


def flat(text):
    """Memory.flat: a multi-line text on one line."""
    return re.sub(r"\s*\n\s*", " ", text) if "\n" in text else text


def bytes_(text):
    return len(text.encode("utf-8"))


def who(rec):
    return "ai" if rec.get("imported") and rec["kind"] in ("unii", "tool") else rec["kind"]


def show(rec):
    """Memory.show (no images here)."""
    return who(rec) + ": " + rec["text"]


def clip512(text):
    """Memory.cut: the first 512 bytes, no broken character."""
    return text.encode("utf-8")[:NODE].decode("utf-8", "ignore")


def tool_text(name, args):
    return name + " " + jdump(args)


def note_for(s):
    return dict(kind="note", text=f"{s.label} session in {s.where}, from {s.start[:16].replace('T', ' ')} (imported)",
                date=s.start)


def build(sessions, opt):
    """hermes.Session list -> OutSession list (tool output clipped)."""
    outs = []
    src, agent = opt.get("source", "Hermes"), opt.get("agent", "agent")
    for s in sessions:
        os_ = OutSession(s.id, f"{src} ({agent}, {s.source})", f"{src.lower()}/{s.source}/{s.id}", s.msgs[0].date)
        for m in s.msgs:
            if m.kind == "tool":
                args = cut_args(m.args, opt["max_call_chars"])
                o = Out("tool", tool_text(m.tool, args), m.date, m.hid, m.tool, args)
            else:
                t = cut(m.text, opt["max_tool_chars"]) if m.kind == "echo" else m.text
                o = Out(m.kind, t, m.date, m.hid, m.tool)
            if o.text.strip():
                os_.msgs.append(o)
        outs.append(os_)
    return outs


def records(outs):
    """Sessions -> Unii message records without positions: per session a note, then its paged messages."""
    for s in outs:
        yield s.id, dict(note_for(s), imported=True)
        for o in s.msgs:
            ps = pages(well_formed(o.text))
            for k, p in enumerate(ps):
                rec = dict(kind=o.kind, text=p, imported=True)
                if k < len(ps) - 1:
                    rec["more"] = True
                rec["date"] = o.date
                yield s.id, rec


# ---- the importer's cost guess (import_guess / node_usd / node_line / node_merge)
def _usd(node):
    if node[0] <= NODE:
        return 0
    return round(PRICE["base"] + PRICE["byte"] * math.log2(node[0] / NODE) + PRICE["depth"] * (node[1] + 1))


def guess(a, sizes):
    """Per level, per node, micro-dollars; sizes = leaf source bytes of messages a, a+1, ..."""
    nodes = [(b, 0) for b in sizes]
    levels, i, l = [], a, 0
    while nodes:
        levels.append([_usd(n) for n in nodes])

        def line(k, nodes=nodes, l=l):
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
    return levels


def guess_usd(a, sizes):
    return sum(sum(x) for x in guess(a, sizes)) / 1e6
