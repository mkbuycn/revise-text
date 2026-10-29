"""revise-text CLI: run, verify, retry, backup, status, baseline and the finish check (guard)."""
import collections, concurrent.futures as cf, difflib, functools, hashlib, http.client, itertools, json, os, pathlib, re, string, sys, threading, time, unicodedata, urllib.error, urllib.request, uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "_vendor"))
from markdown_it import MarkdownIt                   # bundled: markdown-it-py 3.0.0 (MIT), a CommonMark parser

ROOT = pathlib.Path(".revise-text")
HOME = pathlib.Path(os.path.expanduser("~/.revise-text"))
SKILL = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS = {"targets": [], "enforce": False, "pass_threshold": 0.5, "check_draft": True,
            "stealth_attempts": 2, "retries": 1, "length_ratio": [0.8, 1.25], "min_words": 50, "log_text": True,
            "pangram_model": "pangram-4.1", "stealth_model": "super", "stealth_via": "mcp"}
FILE_NAMES = (r"\b[\w-]+\.(?:md|txt|csv|tsv|jsonl?|ya?ml|toml|ini|cfg|conf|log|py|ipynb|js|mjs|ts|tsx|jsx|sh|bash|"
              r"go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|sql|html?|css|xml|gz|zip|tar|png|jpe?g|gif|svg|pdf|env|lock|"
              r"parquet|db|sqlite)\b")
CURRENCY = r"$\u00a2\u00a3\u00a5\u058f\u060b\u09f3\u0e3f\u17db\u20a0-\u20c0\ufdfc\ufe69\uff04\uffe0\uffe1\uffe5\uffe6"
NUMBER = (rf"(?<![^\W_])(?<!\.)[-+\u2212\u00b1{CURRENCY}]?\d(?:[\d.,:]*\d)?"
          rf"(?:%|[{CURRENCY}]|[A-Za-z]{{1,3}}(?![^\W_]))?(?![^\W_])")   # 500, -5, $20, 20€, 50%, 512MB
PATTERNS = [(r"<!--.*?-->", re.S),                  # HTML comments (code, tables, headings: blocks(); `code`: code_spans())
            (r"</?[A-Za-z][\w-]*(?:\s[^<>\n]*)?/?>", 0),   # HTML tags
            (r"!?\[[^\]\n]*\]\([^)\n]*\)", 0),       # whole links and images
            (r"!?\[[^\]\n]+\](?:\[[^\]\n]*\])?", 0),     # [reference][links], [shortcuts], [^notes], [ ] boxes
            (r"https?://[^\s<>]*[^\s<>.,;:!?'\")\]]", 0),  # URLs, without trailing punctuation
            (r"(?<![\w.])~?/[\w.\-/]*[\w\-/]", 0),   # absolute and ~/ paths (not a trailing full stop)
            (r"(?<![\w/])\.{1,2}/[\w.\-/]*[\w\-/]", 0),   # ./ and ../ paths
            (r"\b[\w.-]+/[\w.\-/]*[\w\-]", 0),      # relative paths: data/raw, logs/app.log (prose pairs: exact_items)
            (r"\b[\w.-]+(?:/[\w.-]+)*/(?=[\s.,;:!?)\]'\"]|$)", re.M),   # directories: build/, src/lib/
            (FILE_NAMES, 0),
            (r"(?<![\w.])(?=[\w.]*\d)[A-Za-z0-9](?:[\w.]*[A-Za-z0-9])?(?!\w)", 0),   # identifiers with digits: v1.2, sha256
            (r"\b\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?\b", 0),   # dates (one item: 2026-11-05)
            (NUMBER, 0),                                  # numbers, with sign, currency, % or unit
            (r"^[ \t]*\|.*\|[ \t]*$", re.M)]        # lines that look like table rows, even outside a table
MD = MarkdownIt("commonmark").enable("table")
INVISIBLE = re.compile(r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2069"
                       r"\u3164\ufe00-\ufe0f\ufeff\uffa0\U000e0000-\U000e007f]")
TOKEN = re.compile(r"\[\[[A-Z]+\d+\]\]")
TAGS = ["E", "X", "Q", "Z"] + [f"E{a}{b}" for a in string.ascii_uppercase for b in string.ascii_uppercase]
# a character that changes an item when it appears or disappears right next to it ("-5", "~200", "200+")
SIGNIFICANT_BEFORE = set("~-+<>$@`[]!#_/\\=&^|%\u2212\u00b1\u2248\u2264\u2265\u20ac\u00a3\u00a5\u00d7")
SIGNIFICANT_AFTER = set("+%<>`]_/\\=&^|~@#\u00b0\u00d7\u2248\u2026")        # not "-": "200-row" is fine
YAML_LINE = r"(?:[ \t]*|[ \t]*#[^\n]*|[ \t]*[^\s#:-][^\n]*?:(?:[ \t][^\n]*)?|[ \t]+[^\n]*|[ \t]*-(?:[ \t][^\n]*)?)\n"
FRONTMATTER = re.compile(r"---[ \t]*\n(?=[ \t]*#|[ \t]*[^\s#:-][^\n]*?:[ \t\n]|(?:---|\.\.\.)[ \t]*(?:\n|$))"
                         rf"(?:{YAML_LINE})*?(?:---|\.\.\.)[ \t]*(?:\n|$)"        # YAML: a key or comment first, YAML lines
                         r"|\+\+\+[ \t]*\n[\s\S]*?\+\+\+[ \t]*(?:\n|$)")          # TOML
START = re.compile(r"^<!-- revise:start id=([\w-]+) -->[ \t]*$", re.M)
END = re.compile(r"^<!-- revise:end -->[ \t]*$", re.M)
MARKER_LINES = re.compile(r"^<!-- revise:(?:start id=[\w-]+|end) -->[ \t]*\n?", re.M)
VARIANTS = [{"preservationMode": "inline"}, {"preservationMode": "split"}]   # attempt n uses VARIANTS[n - 1]
PANGRAM = "https://text.external-api.pangram.com"
STEALTH = "https://www.stealthgpt.ai/api/stealthify"
STEALTH_MCP = "https://www.stealthgpt.ai/api/mcp/mcp"
MCP_POLL_SECONDS = 5                                 # StealthGPT suggests polling runs every 5-15 seconds
STEALTH_MAX_WORDS = 3000
STEALTH_USD_PER_WORD = 0.0005                        # super: $0.05 per 100 words, input plus output

# ---------- files, config, history ----------
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Doc:
    """A Markdown file read as LF text without a BOM. Edits are spliced into the original text, so everything
    outside them (BOM, CRLF or mixed line endings) stays byte-for-byte the same."""
    def __init__(self, p):
        self.raw = pathlib.Path(p).read_bytes().decode("utf-8")
        chars, self.pos = [], []
        i = 1 if self.raw.startswith("\ufeff") else 0
        while i < len(self.raw):
            if self.raw.startswith("\r\n", i):       # the LF stands for the whole CRLF
                chars.append("\n"); self.pos.append(i); i += 2
            else:
                chars.append(self.raw[i]); self.pos.append(i); i += 1
        self.pos.append(len(self.raw))
        self.text = "".join(chars)
        self.crlf = self.raw.count("\r\n") * 2 > self.raw.count("\n")

    def splice(self, edits):                          # edits: (start, end, new text) in self.text offsets
        out, last = [], 0
        for a, b, new in sorted(edits):
            ra, rb = self.pos[a], self.pos[b]
            span = self.raw[ra:rb]
            crlf = span.count("\r\n") * 2 > span.count("\n") if "\n" in span else self.crlf
            out += [self.raw[last:ra], new.replace("\n", "\r\n") if crlf else new]
            last = rb
        return "".join(out) + self.raw[last:]

def fkey(p):                                         # from the path inside the project, so stamps can live in git
    full = pathlib.Path(p).resolve()
    try:
        name = full.relative_to(pathlib.Path.cwd().resolve()).as_posix()
    except ValueError:
        name = str(full)
    return hashlib.sha1(name.encode()).hexdigest()[:12]

def config():
    p = ROOT / "config.json"
    try:
        return {**DEFAULTS, **(json.loads(p.read_text(encoding="utf-8")) if p.exists() else {})}
    except ValueError as e:
        sys.exit(f"{p.as_posix()} isn't valid JSON: {e}")

def spec_for(p):
    return ROOT / "specs" / (pathlib.Path(p).as_posix().replace("/", "--") + ".json")

def load_spec(p):
    s = spec_for(p)
    if not s.exists():
        sys.exit(f"No spec for {p}. Save one as {s.as_posix()} first.")
    return json.loads(s.read_text(encoding="utf-8"))

def targets():                                       # never rt's own backups or pasted copies
    return sorted({f for pat in config()["targets"] for f in pathlib.Path(".").glob(pat)
                   if f.is_file() and ROOT.name not in f.parts})

def versions():
    def field(path, key):
        try:
            return json.loads(path.read_text(encoding="utf-8"))[key]
        except (OSError, ValueError, KeyError):
            return "unknown"
    return {"plugin_version": field(SKILL.parent.parent / ".claude-plugin" / "plugin.json", "version"),
            "humanize_writing_commit": field(SKILL / "references" / "humanize-writing" / "UPSTREAM.json", "commit")}

def log(record):
    HOME.mkdir(parents=True, exist_ok=True)
    with open(HOME / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

# ---------- regions and exact items ----------
@functools.lru_cache(maxsize=256)
def structure(text):
    """Block structure from a CommonMark parser (with GFM tables), as (start, end) character spans of whole lines:
    code (fenced or indented), raw (HTML comments, <pre>, <script>...), tables, and headings as
    (start, end, "## Text", level, is_atx, is_top_level). The frontmatter is blanked out so it isn't read as Markdown."""
    fm = FRONTMATTER.match(text)
    body = re.sub(r"[^\r\n]+", "", text[:fm.end()]) + text[fm.end():] if fm else text
    breaks = list(re.finditer(r"\r\n?|\n", text))      # the line breaks markdown-it counts
    starts = [0] + [m.end() for m in breaks]
    def span(lines):
        a = starts[lines[0]]
        b = breaks[lines[1] - 1].start() if lines[1] - 1 < len(breaks) else len(text)
        return a, a + len(text[a:b].rstrip())
    out = {"code": [], "raw": [], "tables": [], "headings": []}
    tokens = MD.parse(body)
    for k, t in enumerate(tokens):
        if not t.map:
            continue
        if t.type in ("fence", "code_block"):
            out["code"].append(span(t.map))
        elif t.type == "html_block" and re.match(r"\s*<(!|\?|(pre|script|style|textarea)\b)", t.content, re.I):
            out["raw"].append(span(t.map))
        elif t.type == "table_open":
            out["tables"].append(span(t.map))
        elif t.type == "heading_open":
            level = int(t.tag[1])
            out["headings"].append((*span(t.map), f"{'#' * level} {tokens[k + 1].content}".strip(), level,
                                    t.markup.startswith("#"), t.level == 0))
    out["refdefs"] = [m.span() for m in re.finditer(r"^ {0,3}\[[^\]\n]+\]:[ \t]*\S[^\n]*$", text, re.M)
                      if not inside(out["code"] + out["raw"], m.start())]   # [label]: https://...
    return out

def inside(spans_, i):
    return any(a <= i < b for a, b in spans_)

def code_spans(text):                                # `code`, ``co`de``, and spans that wrap onto the next line
    out, runs, k = [], [m.span() for m in re.finditer(r"`+", text)], 0
    while k < len(runs):
        a, b = runs[k]
        for j in range(k + 1, len(runs)):
            c, d = runs[j]
            if re.search(r"\n[ \t]*\n", text[b:c]):
                break                                # a code span never crosses a blank line
            if d - c == b - a:
                out.append((a, d))
                k = j
                break
        k += 1
    return out

def markers(text):
    """{id: (content start, content end, block start, block end)}; raises ValueError on a malformed block."""
    code = structure(text)["code"]
    events = sorted([(m.start(), "start", m) for m in START.finditer(text) if not inside(code, m.start())] +
                    [(m.start(), "end", m) for m in END.finditer(text) if not inside(code, m.start())])
    blocks_, open_ = {}, None
    for _, kind, m in events:
        if kind == "start":
            if open_:
                raise ValueError(f"revise:start id={m[1]} is inside block {open_[1]}; blocks can't nest")
            if m[1] in blocks_:
                raise ValueError(f"two blocks have id={m[1]}")
            open_ = m
        elif not open_:
            raise ValueError("a <!-- revise:end --> has no matching revise:start")
        else:
            a = open_.end() + 1
            blocks_[open_[1]] = (a, max(a, m.start() - 1), open_.start(), m.end())
            open_ = None
    if open_:
        raise ValueError(f"revise:start id={open_[1]} has no <!-- revise:end -->")
    return blocks_

def blocks(text):                                    # things that are protected whole and keep their own lines
    st = structure(text)
    try:
        marked = [(oa, ob) for _, _, oa, ob in markers(text).values()]
    except ValueError:
        marked = []                                  # spans() refuses a file whose markers are malformed
    return st["code"] + st["raw"] + st["tables"] + st["refdefs"] + [h[:2] for h in st["headings"]] + marked

def norm_heading(sel):
    m = re.match(r"\s*(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$", sel)
    return f"{m[1]} {m[2] or ''}".strip() if m else sel.strip()

def spans(text, spec):
    """{id: (start, end, outer start, outer end)}. The outer span is what verify replaces (it includes markers)."""
    out = {}
    try:
        marked = markers(text)
    except ValueError as e:
        sys.exit(f"Markers: {e}. Fix the revise markers in the file first.")
    if dup := sorted(i for i, n in collections.Counter(r["id"] for r in spec["regions"]).items() if n > 1):
        sys.exit(f"Region ids must be unique; repeated: {dup}")
    hs = [h for h in structure(text)["headings"] if h[5]]   # top-level headings; ones in lists or quotes don't count
    for r in spec["regions"]:
        sel, rid = r["select"], r["id"]
        if sel == "whole":
            fm = FRONTMATTER.match(text)
            out[rid] = (fm.end() if fm else 0, len(text))
        elif sel == "marker":
            if rid not in marked:
                sys.exit(f"{rid}: no <!-- revise:start id={rid} --> ... <!-- revise:end --> block in the file")
            out[rid] = marked[rid]
            continue
        else:                                        # an ATX heading such as "## Overview"
            want = norm_heading(sel)
            found = [h for h in hs if h[4] and h[2] == want]
            if len(found) != 1:
                sys.exit(f"{rid}: heading {sel!r} " + ("not found" if not found else
                         f"appears {len(found)} times; wrap the one you mean in a marker block"))
            here = found[0]
            eol = re.compile(r"\r\n?|\n").search(text, here[1])
            start = eol.end() if eol else len(text)
            nxt = next((h[0] for h in hs if h[0] > here[0] and h[3] <= here[3]), len(text))
            out[rid] = (start, max(start, nxt))
        out[rid] += out[rid]
    for rid, (a, b, oa, ob) in out.items():         # a region may hold a marker block whole, never cut one
        for bid, (_, _, ma, mb) in marked.items():
            if bid != rid and a < mb and b > ma and not (a <= ma and mb <= b):
                sys.exit(f"Region {rid} cuts through marker block {bid}. Select whole blocks.")
    ordered = sorted(out.items(), key=lambda kv: kv[1][2])
    for (r1, s1), (r2, s2) in zip(ordered, ordered[1:]):
        if s2[2] < s1[3]:
            sys.exit(f"Regions {r1} and {r2} overlap. Select parts of the file that don't overlap.")
    return out

def region_range(spec, rid):                         # a region's word_range, or the spec's when it has one region
    r = next(r for r in spec["regions"] if r["id"] == rid)
    return r.get("word_range") or (spec.get("word_range") if len(spec["regions"]) == 1 else None)

def spec_items(spec):
    """Strings the spec says to keep. A backticked plain word (`count`) is kept in its backticked form only, so the
    same word in ordinary prose can still be rewritten."""
    words = re.findall(r"`([^`\n]+)`", " ".join(spec.get("requirements", [])))
    return set(spec.get("exact", [])) | {f"`{w}`" if re.fullmatch(r"[a-z]+", w) else w for w in words}

PROSE_PAIRS = {"and/or", "either/or", "read/write", "input/output", "on/off", "yes/no", "true/false", "he/she",
               "his/her", "him/her", "s/he", "pass/fail", "start/stop", "open/close", "client/server", "w/o",
               "before/after", "pros/cons", "do/don't", "inbound/outbound", "upload/download", "login/logout",
               "w/", "and/", "or/"}

def exact_items(draft, spec):
    items = spec_items(spec) | {draft[a:b] for a, b in blocks(draft) + code_spans(draft)}
    for p, flags in PATTERNS:
        items.update(m.group(0) for m in re.finditer(p, draft, flags) if m.group(0).lower() not in PROSE_PAIRS)
    return sorted((i for i in items if i.strip()), key=len, reverse=True)

def item_pattern(item):                              # "1" must not match inside "R1" or "10" (but may in _1_)
    head = r"(?<![^\W_])" if re.match(r"[^\W_]", item) else ""
    tail = r"(?![^\W_])" if re.search(r"[^\W_]$", item) else ""
    return head + re.escape(item) + tail

def fragments(text, items):
    """[(start, end)] of every occurrence of every item, in order. Overlapping occurrences merge into one."""
    items = {i.strip() for i in items} - {""}       # an item at a region's edge loses its outer whitespace
    merged = []
    for a, b in sorted({m.span() for i in items for m in re.finditer(item_pattern(i), text)}):
        if merged and a < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged

def protect(text, items):
    """Replace each protected fragment with a token, so no item is ever split across a token and plain text."""
    tag = next(t for t in TAGS if f"[[{t}" not in text)
    tokens, out, last = {}, [], 0
    for a, b in fragments(text, items):
        frag = text[a:b]
        tokens.setdefault(frag, f"[[{tag}{len(tokens)}]]")
        out += [text[last:a], tokens[frag]]
        last = b
    return "".join(out) + text[last:], {tok: frag for frag, tok in tokens.items()}

def restore(text, mapping):
    for token, item in mapping.items():
        text = text.replace(token, item)
    return text

def item_changes(before, after, items, ordered=True):
    """(lost, problems): the protected fragments of `before` must appear in `after` unchanged (in the same order
    when `ordered`; StealthGPT's REST preservation mode keeps it, its MCP tool doesn't),
    with nothing new attached to them, and block items (headings, table rows, code blocks) still on their own
    lines. Fragments the rewriter wrote out itself instead of using a token count too."""
    found = set(items) | {i for i in exact_items(after, {})           # "train/test" written by the rewriter is prose
                          if i in before or not re.fullmatch(r"[A-Za-z]+(?:/[A-Za-z]+)+", i)}
    fb, fa = fragments(before, found), fragments(after, found)
    tb, ta = [before[a:b] for a, b in fb], [after[a:b] for a, b in fa]
    if collections.Counter(tb) != collections.Counter(ta):
        lost = list((collections.Counter(tb) - collections.Counter(ta)).elements())
        added = list((collections.Counter(ta) - collections.Counter(tb)).elements())
        return lost, [f"added {json.dumps(x, ensure_ascii=False)}" for x in added]
    if ordered and tb != ta:
        return [], ["protected items reordered"]
    later = collections.defaultdict(list)            # unordered: the k-th copy of an item pairs with the k-th
    for (a, b), t in zip(fa, ta):
        later[t].append((a, b))
    pairs = list(zip(fb, fa)) if ordered else [((a, b), later[t].pop(0)) for (a, b), t in zip(fb, tb)]
    problems, block_spans = [], blocks(before)
    starts_b, ends_b = {a for a, _ in fb}, {b for _, b in fb}
    starts_a, ends_a = {a for a, _ in fa}, {b for _, b in fa}
    is_block = [any(a1 <= a < b1 or a <= a1 < b for a, b in block_spans) for a1, b1 in fb]
    if not ordered:                                  # items may move between sentences, not across sections
        moved = [pa[0] for (_, pa), blk in zip(pairs, is_block) if blk]
        if moved != sorted(moved):
            problems.append("headings, tables or code blocks changed order")
        for i, ((_, pa), blk) in enumerate(zip(pairs, is_block)):
            if not blk and sum(is_block[:i]) != sum(1 for m in moved if m < pa[0]):
                problems.append(f"{tb[i][:40]!r} moved to another section")
    for ((a1, b1), (a2, b2)), blk in zip(pairs, is_block):
        frag = before[a1:b1]
        if blk:
            # a heading, table or code block: same indent, alone on its line, same blank lines around it
            if line_prefix(after, a2) != line_prefix(before, a1) or \
                    line_suffix(after, b2).strip() != line_suffix(before, b1).strip():
                problems.append(f"{frag[:40]!r} is no longer on its own line as before")
            else:                                    # a lost blank line only matters if it changes the rendering
                lost_above = blank_before(before, a1) and not blank_before(after, a2)
                lost_below = blank_after(before, b1) and not blank_after(after, b2)
                if lost_above or lost_below:
                    fixed, below = after, after.find("\n", b2)
                    if lost_below:
                        fixed = fixed[:below + 1] + "\n" + fixed[below + 1:] if below >= 0 else fixed + "\n"
                    if lost_above:
                        top = after.rfind("\n", 0, a2) + 1
                        fixed = fixed[:top] + "\n" + fixed[top:]
                    if rendered(fixed) != rendered(after):
                        problems.append(f"{frag[:40]!r} lost a blank line next to it")
            continue
        for side, was, now in (("before", context(before, a1, b1, "before", ends_b), context(after, a2, b2, "before", ends_a)),
                               ("after", context(before, a1, b1, "after", starts_b), context(after, a2, b2, "after", starts_a))):
            if was[0] != now[0] and (significant(*was, side, frag) or significant(*now, side, frag)):
                problems.append(f"{frag[:40]!r}: {was[0]!r} {side} it became {now[0]!r}")
    return [], problems

def context(text, a, b, side, other):
    """(the character beside the item, the one beyond it, whether the item is wrapped in *emphasis* or _emphasis_,
    whether that character is a "/" joining it to another protected item: `ERROR`/`WARN`). `other` holds where
    the other items end (side "before") or start (side "after")."""
    c = text[a - 1:a] if side == "before" else text[b:b + 1]
    emph = (text[a - 1:a] in ("*", "_") and text[a - 1:a] == text[b:b + 1]) or in_emphasis(text, a, b, c, side)
    if side == "before":
        return text[a - 1:a], text[a - 2:a - 1], emph, text[a - 1:a] == "/" and a - 1 in other
    return text[b:b + 1], text[b + 1:b + 2], emph, text[b:b + 1] == "/" and b + 1 in other

def in_emphasis(text, a, b, c, side):
    """Whether c (a single * or _ beside the item) opens or closes emphasis that runs on past the item within its
    paragraph: "*at most 200* rows" -> the * after 200 closes an emphasis opened earlier."""
    if c not in ("*", "_") or (text[b + 1:b + 2] if side == "after" else text[a - 2:a - 1]) == c:
        return False
    start, end = text.rfind("\n\n", 0, a) + 1, text.find("\n\n", b)
    span = text[start:a - 1] if side == "after" else text[b:end if end >= 0 else len(text)]
    singles = re.findall(r"(?<!\w)_(?!_)|(?<!_)_(?!\w)" if c == "_" else r"(?<!\*)\*(?!\*)", span)
    return len(singles) % 2 == 1

def significant(c, beyond, emph, joined, side, frag):
    """Whether character c beside an item changes what it means: a sign, unit, glob, suffix or attached letter.
    `beyond` is the next character out. A dash joining words ("150–200", "cap—200") isn't a sign; a dash after a
    space is ("–40"). A hyphen after a number ("200-row") is fine; ".bak" or "-beta" after a name isn't."""
    if not c or joined or (emph and c in "*_"):
        return False
    if side == "before" and frag.startswith("["):  # a footnote or link means the same after any word or sign
        return c == "!"                              # (35%[^1]), unless a "!" turns a link into an image
    if c.isalnum() or unicodedata.category(c) in ("Mn", "Mc", "Me", "Sc"):   # letters, marks, currencies
        return True
    n, cat = unicodedata.normalize("NFKC", c), unicodedata.category(c)
    if side == "before":
        if c == "." and frag[:1].isdigit():         # 5 -> .5
            return True
        if (cat == "Pd" or n == "-") and beyond.isalnum():
            return False
        return n in SIGNIFICANT_BEFORE or cat in ("Pd", "Sm")
    if c == "*":
        return beyond != "*"
    if c in ".-" and beyond.isalnum() and frag[-1:].isalnum() and not (c == "-" and re.fullmatch(NUMBER, frag)):
        return True
    return n in SIGNIFICANT_AFTER or cat == "Sm"

def line_prefix(text, i):
    return text[text.rfind("\n", 0, i) + 1:i]

def line_suffix(text, i):
    j = text.find("\n", i)
    return text[i:j if j >= 0 else len(text)]

def blank_before(text, i):                           # the line above is blank, or there is none
    k = text.rfind("\n", 0, i)
    return k < 0 or not text[text.rfind("\n", 0, k) + 1:k].strip()

def blank_after(text, i):                            # the line below is blank, or there is none
    k = text.find("\n", i)
    return k < 0 or not text[k + 1:(text.find("\n", k + 1) + 1 or len(text) + 1) - 1].strip()

def split_ws(text):
    """(leading blank lines, core, trailing whitespace). The core keeps its first line's indent, which can matter
    (an indented code block)."""
    core = text.rstrip()
    if not core.strip():
        return text, "", ""
    start = core.rfind("\n", 0, len(core) - len(core.lstrip())) + 1
    return text[:start], core[start:], text[len(core):]

# ---------- APIs ----------
class SentError(RuntimeError):
    """The request may have reached the server (a dropped connection, a timeout, a 5xx), so billed work may run."""

def request(url, headers, body=None, tries=3, safe=True):
    """JSON over HTTP with retries. An unsafe call (one that starts billed work) is only retried after 429."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None,
                                         {"Content-Type": "application/json", **headers})
            return json.load(urllib.request.urlopen(req, timeout=120))
        except urllib.error.HTTPError as e:
            if i == tries - 1 or not (e.code == 429 or (safe and e.code in (500, 502, 503, 504))):
                kind = SentError if e.code >= 500 else RuntimeError
                raise kind(f"{url} returned {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            if not safe or i == tries - 1:
                raise SentError(f"{url}: connection failed ({e})") from None
        time.sleep(2 * 2 ** i)

def pangram(text, model):
    headers = {"x-api-key": os.environ["PANGRAM_KEY"]}
    try:
        task = request(f"{PANGRAM}/task", headers, {"text": text, "model": model}, safe=False)["task_id"]
    except RuntimeError as e:
        if " returned 402" in str(e):
            raise RuntimeError("Pangram API has no credits left. API credits are separate from Pangram's web plans; "
                               "buy them at https://www.pangram.com/solutions/api") from None
        raise
    for _ in range(120):
        res = request(f"{PANGRAM}/task/{task}", headers)
        if res.get("stage") == "STAGE_SUCCESS":
            return {"pangram_ai": res["fraction_ai"], "pangram_ai_assisted": res.get("fraction_ai_assisted"),
                    "humanized_windows": sum(bool(w.get("is_humanized")) for w in res.get("windows", []))}
        if res.get("stage") == "STAGE_FAILED":
            raise RuntimeError(f"Pangram failed: {res.get('headline')}")
        time.sleep(1)
    raise TimeoutError("Pangram did not finish within 2 minutes")

def stealth(text, variant, model):
    """(rewritten text, words charged, billing mode). variant["via"] picks the route: "mcp" goes through
    StealthGPT's MCP server, which bills the web subscription when its "MCP billing" setting says so; "api" is
    the REST API, which always bills API credits."""
    variant = dict(variant)
    if variant.pop("via", "api") == "mcp":
        return stealth_mcp(text, variant, model)
    try:
        res = request(STEALTH, {"api-token": os.environ["STEALTH_KEY"]},
                      {"prompt": text, "rephrase": True, "model": model, "preservation": True,
                       "outputFormat": "markdown", **variant}, safe=False)
    except SentError as e:
        raise StealthRunError(str(e), None, 2 * len(text.split())) from e
    return res["result"], res.get("wordsSpent"), res.get("billingMode"), res.get("meteredChargedCredits")

class Mcp:
    """A small client for StealthGPT's MCP server: Streamable HTTP, JSON-RPC 2.0, replies as JSON or SSE.
    Regions run in parallel share one client and one session."""
    HANDSHAKE = ("initialize", "notifications/initialized", "tools/list")

    def __init__(self, url, token):
        self.url, self.token, self.session, self.tools = url, token, None, None
        self.ids, self.lock = itertools.count(1), threading.Lock()

    def post(self, payload, tries=3):
        method = payload.get("method")
        unsafe = method == "tools/call" and payload["params"]["name"] == "humanize_text"   # a repeat may bill again
        for i in range(tries):
            sent = self.session
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "Authorization": f"Bearer {self.token}", "MCP-Protocol-Version": "2025-06-18",
                       **({"Mcp-Session-Id": sent} if sent else {})}
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(payload).encode(), headers),
                                            timeout=120) as r:
                    self.session = r.headers.get("Mcp-Session-Id") or self.session
                    body, kind = r.read().decode("utf-8", "replace"), r.headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as e:
                if e.code == 404 and sent and method not in self.HANDSHAKE:
                    with self.lock:                  # the session expired; the first request to notice starts anew
                        if self.session == sent:
                            self.handshake()
                    continue
                if i == tries - 1 or not (e.code == 429 or (not unsafe and e.code in (500, 502, 503, 504))):
                    kind = SentError if e.code >= 500 else RuntimeError
                    raise kind(f"StealthGPT MCP returned {e.code}: "
                               f"{e.read()[:200].decode('utf-8', 'replace')}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
                if unsafe or i == tries - 1:
                    raise SentError(f"StealthGPT MCP: connection failed ({e})") from None
            time.sleep(2 * 2 ** i)
        else:
            raise RuntimeError(f"StealthGPT MCP: no answer after {tries} tries")
        if "id" not in payload:
            return None                              # a notification has no reply
        chunks = ["\n".join(l[5:].lstrip() for l in e.splitlines() if l.startswith("data:"))
                  for e in re.split(r"\r?\n\r?\n", body)] if "event-stream" in kind else [body]
        messages = []
        for chunk in chunks:                         # skip keep-alives and anything else that isn't JSON
            try:
                m = json.loads(chunk)
            except ValueError:
                continue
            messages += m if isinstance(m, list) else [m]
        reply = next((m for m in messages if isinstance(m, dict) and m.get("id") == payload["id"]
                      and "method" not in m), None)  # a server request may reuse our id
        if reply is None:
            raise RuntimeError("StealthGPT MCP sent no reply")
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(f"StealthGPT MCP: {err.get('message', err) if isinstance(err, dict) else err}")
        return reply.get("result") or {}

    def call(self, method, params):
        return self.post({"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params})

    def start(self):
        with self.lock:
            if self.tools is None:
                self.handshake()

    def handshake(self):                             # with self.lock held: a new session, then the tools' inputs
        self.session, self.tools = None, None
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "revise-text", "version": versions()["plugin_version"]}})
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = {t["name"]: t.get("inputSchema", {}).get("properties", {})
                      for t in self.call("tools/list", {}).get("tools", [])}

    def tool(self, name, args):
        self.start()
        res = self.call("tools/call", {"name": name, "arguments": args})
        data = res.get("structuredContent")
        for c in res.get("content", []) if data is None else []:
            if c.get("type") == "text":
                try:
                    data = json.loads(c["text"])
                except ValueError:
                    data = {"message": c["text"]}
                break
        if res.get("isError"):
            raise RuntimeError(f"StealthGPT MCP {name}: {str(data)[:200]}")
        return data if isinstance(data, dict) else {}

class StealthRunError(RuntimeError):
    """A StealthGPT run that started, and so may have been billed, but didn't finish."""
    def __init__(self, message, run_id, words):
        super().__init__(message)
        self.run_id, self.words = run_id, words

MCP_CLIENTS = {}

def stealth_mcp(text, variant, model):
    client = MCP_CLIENTS.setdefault(STEALTH_MCP, Mcp(STEALTH_MCP, os.environ["STEALTH_KEY"]))
    client.start()
    inputs = client.tools.get("humanize_text")
    if inputs is None:
        raise RuntimeError(f"StealthGPT's MCP server has no humanize_text tool (it has {sorted(client.tools)})")
    args = {"text": text, "model": model, "outputFormat": "markdown"}
    if "qualityMode" in inputs:
        args["qualityMode"] = "quality"
    if "preservation" in inputs:                     # undocumented for MCP; used whenever the tool accepts it
        args.update(preservation=True, **{k: v for k, v in variant.items() if k in inputs})
    try:
        run = client.tool("humanize_text", args)
    except SentError as e:
        raise StealthRunError(str(e), None, 2 * len(text.split())) from e
    run_id = run.get("runId") or run.get("run_id")
    if not run_id:
        raise RuntimeError(f"StealthGPT MCP started no run: {str(run)[:200]}")
    try:
        for _ in range(int(600 / max(MCP_POLL_SECONDS, 0.01))):
            time.sleep(MCP_POLL_SECONDS)
            st = client.tool("get_run_status", {"runId": run_id})
            if isinstance(st.get("result"), dict):   # some replies nest the run's fields under "result"
                st = {**st, **st["result"]}
            if st.get("status") == "completed":
                if not isinstance(st.get("result"), str):
                    raise StealthRunError(f"StealthGPT run {run_id} finished without text: {str(st)[:150]}", run_id,
                                          st.get("wordsSpent") or 2 * len(text.split()))
                return (st["result"], st.get("wordsSpent") or st.get("creditsSpent"), st.get("billingMode") or "unknown",
                        st.get("meteredChargedCredits"))
            if st.get("status") in ("failed", "cancelled"):
                raise StealthRunError(f"StealthGPT run {st['status']}: {st.get('error') or st.get('message') or ''}",
                                      run_id, 0)
        raise TimeoutError("StealthGPT run did not finish within 10 minutes")
    except StealthRunError:
        raise
    except Exception as e:                           # it may still finish and be billed: count the words
        raise StealthRunError(f"StealthGPT run {run_id}: {e}", run_id, 2 * len(text.split())) from e

def billed_to(step):
    """Which balance a StealthGPT attempt was charged to: "credits" (API), "plan" (subscription) or "unknown"."""
    billing = step.get("billing")
    if billing in ("prepaid", "payg") or (billing is None and step.get("via", "api") == "api"):
        return "credits"
    return "plan" if billing == "subscription" else "unknown"

# ---------- one region ----------
def humanize(draft, items, cfg, first=1, word_range=None, steps=None, draft_ai=None):
    lead, core, trail = split_ws(draft)
    steps = [] if steps is None else steps           # the caller's list, so an error still reports what was billed
    words, (lo, hi) = len(core.split()), cfg["length_ratio"]
    if words < max(cfg["min_words"], 1):
        return {"status": "SHORT", "out": draft, "steps": steps, "best_ai": None}
    if words > STEALTH_MAX_WORDS:                    # before any check is paid for
        raise ValueError(f"region has {words} words; StealthGPT takes at most {STEALTH_MAX_WORDS}. Split it.")
    if cfg["check_draft"] and first == 1:
        step = {"step": "draft", **pangram(core, cfg["pangram_model"])}
        steps.append(step)
        draft_ai = step["pangram_ai"]
        if draft_ai < cfg["pass_threshold"]:
            return {"status": "PASS", "passed_at": "draft", "out": draft, "steps": steps, "best_ai": draft_ai,
                    "draft_ai": draft_ai}
    best = (draft_ai if draft_ai is not None else float("inf"), draft)   # the draft until something scores better
    in_range = word_range and word_range[0] <= words <= word_range[1]
    for n in range(first, first + cfg["stealth_attempts"]):
        guarded, mapping = protect(core, items)
        try:
            got = stealth(guarded, {**VARIANTS[(n - 1) % len(VARIANTS)], "via": cfg["stealth_via"]}, cfg["stealth_model"])
        except StealthRunError as e:
            steps.append({"step": f"stealth_{n}", "via": cfg["stealth_via"], "run_id": e.run_id,
                          "words_billed": e.words, "billing": "unknown", "error": str(e)[:200]})
            raise
        raw, spent, billing, metered = (tuple(got) + (None, None))[:4]
        keep = set(INVISIBLE.findall(core))          # before restoring, so items keep their own characters;
        raw = INVISIBLE.sub(lambda m: m[0] if m[0] in keep else "", raw.replace("\r\n", "\n"))   # draft's own stay
        out_core = restore(raw, mapping).lstrip("\r\n").rstrip()
        stray = list((collections.Counter(TOKEN.findall(out_core)) - collections.Counter(TOKEN.findall(core))).elements())
        missing, problems = item_changes(core, out_core, items, ordered=cfg["stealth_via"] == "api")
        out, out_words = lead + out_core + trail, len(out_core.split())
        step = {"step": f"stealth_{n}", "words_billed": spent or words + out_words, "via": cfg["stealth_via"],
                **({"billing": billing} if billing else {}), **({"metered_words": metered} if metered else {}),
                "missing": missing,
                "length_ratio": round(out_words / words, 2)}
        if stray or problems:
            step["unexpected"] = stray + problems
        steps.append(step)
        if missing or stray or problems:
            step["rejected"] = "exact items"
            continue
        if not lo <= out_words / words <= hi or (in_range and not word_range[0] <= out_words <= word_range[1]):
            step["rejected"] = "length"
            continue
        step.update(pangram(out_core, cfg["pangram_model"]))
        if step["pangram_ai"] < best[0]:
            best = (step["pangram_ai"], out)
        if step["pangram_ai"] < cfg["pass_threshold"]:
            return {"status": "PASS", "passed_at": step["step"], "out": out, "steps": steps,
                    "best_ai": step["pangram_ai"], "draft_ai": draft_ai}
    return {"status": "REVIEW", "out": best[1], "steps": steps, "draft_ai": draft_ai,
            "best_ai": None if best[0] == float("inf") else best[0]}

def safe_humanize(draft, items, cfg, first=1, word_range=None, draft_ai=None):
    steps = []
    try:
        return humanize(draft, items, cfg, first, word_range, steps, draft_ai)
    except Exception as e:                           # keep the draft; the rest of the file carries on
        return {"status": "ERROR", "error": str(e)[:200], "out": draft, "steps": steps, "best_ai": None,
                "draft_ai": draft_ai}

def result_line(res):
    line = res["status"] + (f" at {res['passed_at']}" if res.get("passed_at") else "")
    line += f" · Pangram AI {res['best_ai']:.2f}" if res.get("best_ai") is not None else ""
    return line + (f" · {res['error']}" if res.get("error") else "")

def save(path, rid, spec, res, cfg, **meta):
    d = ROOT / "work" / fkey(path) / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    words = lambda to: sum(s["words_billed"] for s in res["steps"] if "words_billed" in s and billed_to(s) == to)
    billed = words("credits") + sum(s.get("metered_words", 0) for s in res["steps"] if billed_to(s) != "credits")
    plan_words = words("plan")
    sel = next((r["select"] for r in spec["regions"] if r["id"] == rid), "")
    log({"type": "run", "run_id": res["run_id"], "ts": now(), **versions(), **meta,
         "source": path.as_posix(), "region": rid, "spec": spec_for(path).as_posix(),
         "select": sel if sel in ("whole", "marker") else "heading",
         "pass_threshold": cfg["pass_threshold"], "word_range": res.get("word_range"),
         "input_words": len(res["draft"].split()), "requirements": len(spec.get("requirements", [])),
         "requirement_list": spec.get("requirements", []), "exact_list": spec.get("exact", []),
         "exact_items": len(res["items"]), "steps": res["steps"], "result": res["status"],
         "passed_at": res.get("passed_at"), "best_pangram_ai": res.get("best_ai"), "error": res.get("error"),
         "pangram_checks": sum("pangram_ai" in s for s in res["steps"]),
         "stealth_words_billed": billed, "stealth_cost_usd": round(billed * STEALTH_USD_PER_WORD, 3),
         "stealth_subscription_words": plan_words, "stealth_unknown_billing_words": words("unknown"),
         **({"text_before": res["draft"], "text_after": res["out"]} if cfg["log_text"] else {})})
    print(f"== {path.as_posix()} · {rid} · {result_line(res)}")
    for s in res["steps"]:
        print("  " + " · ".join(f"{k}={v}" for k, v in s.items()))
    # with several regions an item can belong to any of them, so only flag what the whole file lacks
    scope = res["draft"] if len(spec["regions"]) == 1 else Doc(path).text
    absent = sorted(e for e in spec_items(spec) if e not in scope)
    if absent:
        print(f"  WARNING: your draft is missing these spec items: {absent}")
    wr, words = res.get("word_range"), len(res["draft"].split())
    if wr and not wr[0] <= words <= wr[1]:
        print(f"  WARNING: your draft has {words} words; the spec's word_range is {wr}")
    print("Requirements:\n" + "\n".join(f"  {r}" for r in spec.get("requirements", [])))
    print("Revised text:\n" + res["out"].strip("\n") + "\n")

def need_keys():
    missing = [k for k in ("PANGRAM_KEY", "STEALTH_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} in the environment first.")

# ---------- commands ----------
def cmd_run(args):
    need_keys()
    cfg, files = config(), [a for a in args if not a.startswith("--")]
    critique = next((a.split("=", 1)[1] for a in args if a.startswith("--critique=")), "")
    mode = "standalone" if "--standalone" in args else "inline"
    for f in files:
        path, spec = pathlib.Path(f), load_spec(f)
        text = Doc(path).text
        regions = spans(text, spec)
        try:
            unused = sorted(set(markers(text)) - {r["id"] for r in spec["regions"] if r["select"] == "marker"})
        except ValueError:
            unused = []
        if unused:
            print(f"Note: marker blocks not in the spec are kept exactly as they are: {unused}")
        cmd_backup([f])
        jobs = {}
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            for rid, (a, b, _, _) in regions.items():
                draft, wr = text[a:b], region_range(spec, rid)
                items = exact_items(draft, spec)
                jobs[rid] = (draft, items, wr, pool.submit(safe_humanize, draft, items, cfg, 1, wr))
        for rid, (draft, items, wr, fut) in jobs.items():
            res = fut.result()
            res.update(run_id=uuid.uuid4().hex[:12], draft=draft, items=items, word_range=wr, retries=0,
                       attempts=sum(s["step"].startswith("stealth") for s in res["steps"]))
            save(path, rid, spec, res, cfg, mode=mode,
                 critique=[c.strip() for c in critique.split(";") if c.strip()])
        print(f"Verify: rt verify {f} " + " ".join(f"{rid}=ok" for rid in jobs))

def cmd_retry(args):
    need_keys()
    cfg, path, rid = config(), pathlib.Path(args[0]), args[1]
    d = ROOT / "work" / fkey(path) / rid
    if not (d / "result.json").exists():
        sys.exit(f"{rid}: nothing to retry. Run: rt run {args[0]}")
    old = json.loads((d / "result.json").read_text(encoding="utf-8"))
    if old["retries"] >= cfg["retries"]:
        sys.exit(f"{rid}: no retries left. Run: rt verify {args[0]} {rid}=\"what changed\" --final")
    res = safe_humanize(old["draft"], old["items"], {**cfg, "check_draft": False, "stealth_attempts": 1},
                        old["attempts"] + 1, old.get("word_range"), old.get("draft_ai"))
    res.update(run_id=uuid.uuid4().hex[:12], draft=old["draft"], items=old["items"],
               word_range=old.get("word_range"), retries=old["retries"] + 1,
               attempts=old["attempts"] + sum(s["step"].startswith("stealth") for s in res["steps"]))
    save(path, rid, load_spec(path), res, cfg, mode="retry", retry_of=old["run_id"])

def cmd_verify(args):
    path, final = pathlib.Path(args[0]), "--final" in args
    verdicts = dict(a.split("=", 1) for a in args[1:] if "=" in a and not a.startswith("--"))
    doc = Doc(path)
    spec = load_spec(path)
    regions, work = spans(doc.text, spec), ROOT / "work" / fkey(path)
    levels = {r["id"]: len(norm_heading(r["select"]).split()[0]) for r in spec["regions"]
              if r["select"] not in ("whole", "marker")}
    if unknown := sorted(set(verdicts) - set(regions)):
        sys.exit(f"Unknown regions: {unknown}. Regions in the spec: {sorted(regions)}")
    results = {}
    for rid, (a, b, _, _) in regions.items():
        p = work / rid / "result.json"
        if not p.exists():
            sys.exit(f"{rid}: no result yet. Run: rt run {args[0]}")
        results[rid] = json.loads(p.read_text(encoding="utf-8"))
        if results[rid]["draft"] != doc.text[a:b]:
            sys.exit(f"{rid}: the file changed after `rt run`. Run `rt run {args[0]}` again.")
    for rid, res in results.items():                 # a region with no verdict is only recorded when it's final
        if rid in verdicts or (final and not remembered_ok(work, rid, res)):
            v = verdicts.get(rid, "not verified")
            log({"type": "verify", "run_id": res["run_id"], "ts": now(), "verified": v == "ok",
                 "mismatches": [] if v == "ok" else [v], "final": final})
            (work / rid / "verdict.json").write_text(json.dumps({"run_id": res["run_id"], "ok": v == "ok"}))
    pending = [rid for rid in regions if not remembered_ok(work, rid, results[rid])]
    if pending and not final:
        print(f"Not applied. Mismatched or not checked: {pending}. Run `rt retry {args[0]} <region>` "
              f"or verify again with --final to keep your draft for those regions. Regions already marked ok stay ok.")
        return
    order = sorted(regions.items(), key=lambda kv: kv[1][2])
    contents = {rid: results[rid]["out"] if rid not in pending else doc.text[a:b]   # a mismatch keeps the draft,
                for rid, (a, b, _, _) in order}                                     # which has the right meaning
    choices = seams(doc.text, order, contents)
    edits, stamped, delta = [], {}, 0
    for rid, (a, b, oa, ob) in order:
        ok, new = rid not in pending, contents[rid]
        marked = (oa, ob) != (a, b)
        pre, post, ob = choices.get(rid, ("", "", ob))
        if new == doc.text[a:b] and new:             # unchanged: leave its bytes alone, only drop the markers
            edits += [(oa, a, pre), (b, ob, post)] if marked else []
        elif marked or new != doc.text[a:b]:
            edits.append((oa, ob, pre + new + post))
        if not new and marked and post:              # an empty block left as one <!-- --> line
            pre = ""
        stamped[rid] = {"status": results[rid]["status"] if ok else "REVIEW", "text": new,
                        **({"level": levels[rid]} if rid in levels else {}),   # where it sits in the written file:
                        "range": [oa + delta + len(pre), oa + delta + len(pre) + len(new)]}
        delta += len(pre) + len(new) + len(post) - (ob - oa)
        tail = "verified" if ok else f"kept your draft ({verdicts.get(rid, 'not verified')})"
        print(f"{rid}: {result_line(results[rid]) if ok else 'REVIEW'} · {tail}")
    previous = load_stamp(path)
    path.write_bytes(doc.splice(edits).encode("utf-8"))
    covered = [(heading_line_start(doc.text, a), ob) if a == oa else (oa, ob + (doc.text[ob:ob + 1] == "\n"))
               for a, b, oa, ob in regions.values()]
    stamp(path, stamped, previous, doc.text, covered)

def seams(text, order, contents):
    """{region id: (what replaces its start marker line, what replaces its end marker line, end of the replaced
    span)} for each marker region: the least intrusive choice that keeps the document rendering as it would with
    the markers still in, comments aside. A marker is a one-line HTML comment, which ends a paragraph, list or
    quote; where simply removing it would let the region and its neighbour run together, a blank line or an empty
    <!-- --> takes its place. Regions are settled one after another against the choices already made, so two
    blocks written back to back don't merge."""
    def compose(choices):                            # all contents in place; chosen markers replaced, others kept
        out, last = [], 0
        for rid, (a, b, oa, ob) in order:
            if rid in choices:
                pre, post, end = choices[rid]
                out += [text[last:oa], pre, contents[rid], post]
                last = end
            else:
                out += [text[last:a], contents[rid]]
                last = b
        return "".join(out) + text[last:]
    want, choices = rendered(compose({})), {}
    cost = {"": 0, "\n": 1, "<!-- -->\n": 2, "\n<!-- -->": 2}
    for rid, (a, b, oa, ob) in order:
        if (oa, ob) == (a, b):
            continue
        if not contents[rid]:                        # an empty block: drop its two lines, or leave one comment
            end = ob + 1 if text[ob:ob + 1] == "\n" else ob
            options = [("", "", end), ("", "<!-- -->", ob)]
        else:
            options = [(pre, post, ob) for pre, post in sorted(
                itertools.product(("", "\n", "<!-- -->\n"), ("", "\n", "\n<!-- -->")),
                key=lambda pp: cost[pp[0]] + cost[pp[1]])]
        choices[rid] = next((o for o in options if rendered(compose({**choices, rid: o})) == want), options[-1])
    return choices

def rendered(text):                                  # the HTML, without comments
    return re.sub(r"<!--.*?-->\n?", "", MD.render(text), flags=re.S)

def heading_line_start(text, a):                     # a section's heading line counts as part of what was revised
    k = text.rfind("\n", 0, max(a - 1, 0))
    return k + 1 if a else 0

def remembered_ok(work, rid, res):                   # an "ok" holds until the region is run or retried again
    p = work / rid / "verdict.json"
    if not p.exists():
        return False
    v = json.loads(p.read_text(encoding="utf-8"))
    return v["ok"] and v["run_id"] == res["run_id"]

def cmd_backup(args):
    for f in args:
        dest = ROOT / "backups" / f"{fkey(f)}-{time.time_ns() // 1000}{pathlib.Path(f).suffix}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pathlib.Path(f).read_bytes())
        print(f"backup: {dest.as_posix()}")

def stamp(path, regions, previous=None, before=None, covered=()):
    """Record the file as written and where each revised region sits in it, for status and the finish check.
    Regions stamped earlier are carried over to their new place, minus any part revised again: `before` is the
    file's text just before this verify, and `covered` the spans (in that text) this verify revised. Hand edits
    found inside an old region are kept as spans, so only the piece that holds one is reported."""
    text = Doc(path).text
    if previous:
        to_before, to_now = line_diff(previous["text"], before), line_diff(before, text)
        for rid, r in previous["regions"].items():
            a, b = move(to_before, r["range"][0], False), move(to_before, r["range"][1], True)
            edits = [(move(to_before, x, False), move(to_before, y, True)) for x, y in r.get("edits", [])]
            edits += edit_spans(to_before, r, previous["text"], before, covered)
            pieces = [(a, b)]
            for ca, cb in covered:
                pieces = [q for pa, pb in pieces for q in ((pa, min(pb, ca)), (max(pa, cb), pb)) if q[0] < q[1]]
            pieces = [(pa, pb) for pa, pb in pieces if before[pa:pb].strip() or any(pa <= ea <= pb for ea, _ in edits)]
            if not pieces and r["range"][0] < r["range"][1] and (a >= b or not covered_by(a, b, covered)):
                print(f"Note: revised region {rid} is no longer in the file; it isn't tracked any more.")
            for pa, pb in pieces:
                mine = [[move(to_now, max(ea, pa) if ea < pb else ea, False), move(to_now, min(eb, pb) if eb > pa else eb, True)]
                        for ea, eb in edits if (ea == eb and pa <= ea <= pb) or ea == pb or eb == pa or
                        (ea < pb and eb > pa and before[max(ea, pa):min(eb, pb)].strip())]
                na, nb = move(to_now, pa, False), move(to_now, pb, True)
                key, k = rid.split("~")[0], 1
                while key in regions:
                    k += 1
                    key = f"{rid.split('~')[0]}~{k}"
                regions[key] = {**{f: v for f, v in r.items() if f != "edits"}, "range": [na, nb],
                                "text": text[na:nb], **({"edits": mine} if mine else {})}
    (ROOT / "stamps").mkdir(parents=True, exist_ok=True)
    (ROOT / "stamps" / f"{fkey(path)}.json").write_text(json.dumps(
        {"file": pathlib.Path(path).as_posix(), "ts": now(), "regions": regions, "text": text,
         "sha256": hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()}, ensure_ascii=False),
        encoding="utf-8")

def covered_by(a, b, covered):
    return any(ca <= a and b <= cb for ca, cb in covered)

def load_stamp(path):                                # None when there is none or it can't be read
    s = ROOT / "stamps" / f"{fkey(path)}.json"
    try:
        st = json.loads(s.read_text(encoding="utf-8")) if s.exists() else None
    except (ValueError, OSError):
        return None
    return st if isinstance(st, dict) and {"text", "regions", "sha256"} <= set(st) else None

def line_diff(old, new):
    """[(tag, old start, old end, new start, new end)] in characters, from a line-by-line diff. A missing final
    line break is added to both first, so text appended to a file without one isn't seen as changing its last line."""
    a = (old if old.endswith("\n") or not old else old + "\n").splitlines(keepends=True)
    b = (new if new.endswith("\n") or not new else new + "\n").splitlines(keepends=True)
    oa, ob = [0], [0]
    for line in a:
        oa.append(oa[-1] + len(line))
    for line in b:
        ob.append(ob[-1] + len(line))
    c, d = (lambda x: min(x, len(old))), (lambda x: min(x, len(new)))
    return [(tag, c(oa[i1]), c(oa[i2]), d(ob[j1]), d(ob[j2]))
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]

def move(changes, x, end):
    """Where offset x of the old text sits in the new one. Text inserted exactly at x lands after a start (end
    False) and before an end (end True), so it never joins the range."""
    for tag, a0, a1, b0, b1 in changes:
        if tag == "equal":
            if a0 <= x < a1 or (x == a1 and end):
                return b0 + (x - a0)
        elif a0 == a1 == x:
            return b0 if end else b1
        elif a0 <= x < a1:
            return b1 if end else b0
    return changes[-1][4] if changes else x

def edit_spans(changes, region, old_text, new_text, covered=()):
    """Where (in new_text) a change touches the region: text changed or removed inside it, or text added inside
    or at its edge (or on the line right after it). Blank lines don't count, nor do changes inside `covered`
    (spans of new_text this verify revised), nor an insertion that could just as well sit outside the region
    (when lines repeat, the diff's choice is arbitrary), nor a new section of the same or a higher level added
    right after a section."""
    s0, e0 = region["range"]
    edge, level, out = e0 + 1 if old_text[e0:e0 + 1] == "\n" else e0, region.get("level"), []
    for tag, lo, hi, nlo, nhi in changes:
        if tag == "equal":
            continue
        parts = [(nlo, nhi)]
        for ca, cb in covered:
            parts = [q for pa, pb in parts for q in ((pa, min(pb, ca)), (max(pa, cb), pb)) if q[0] < q[1]]
        if nhi > nlo and not parts:                  # all of it is text this verify revised, unless revised
            gone = old_text[lo:hi]                   # text deleted, only markers in its place, and not just the
            whole_block = gone.endswith("\n\n") or not old_text[hi:].split("\n", 1)[0].strip()   # start of that paragraph
            if lo < e0 and hi > s0 and gone.strip() and whole_block and not MARKER_LINES.sub("", new_text[nlo:nhi]).strip():
                out.append((nlo, nlo))
            continue
        text_parts = [(pa, pb) for pa, pb in parts if new_text[pa:pb].strip()]
        seg = new_text[nlo:nhi]
        if lo == hi:                                 # an insertion
            if not text_parts or not s0 <= lo <= edge:
                continue
            if not s0 < lo < e0:                     # at an edge
                if any(not s0 <= q <= edge for q in placements(old_text, lo, seg)):
                    continue
                if level and lo >= e0 and starts_section(new_text, nlo, level):
                    continue
                if lo == 0 and (fm := FRONTMATTER.match(seg)) and not seg[fm.end():].strip():
                    continue                         # frontmatter added at the top
            out += text_parts
        elif lo < e0 and hi > s0 and (old_text[max(lo, s0):min(hi, e0)].strip() or text_parts):
            out += text_parts or [(nlo, nlo)]        # a removal inside leaves a zero-width mark
        elif level and lo <= e0 < hi:                # the heading that ended the section was changed or demoted
            if starts_section(old_text, e0, level) and not starts_section(new_text, nlo, level):
                out += text_parts or [(nlo, nlo)]
    return out

def starts_section(text, i, level):
    """Whether what follows offset i (after blank lines) ends a section of this level: the end of the file, or a
    heading (ATX or setext) of the same or a higher level."""
    j = len(text) - len(text[i:].lstrip("\r\n"))
    return not text[j:].strip() or any(h[0] == j and h[3] <= level for h in structure(text)["headings"])

def touched(changes, rng, old_text, new_text, covered=()):
    return bool(edit_spans(changes, {"range": rng}, old_text, new_text, covered))

def placements(old, p, inserted):
    """Every offset where inserting `inserted` (whole lines) at line boundary p gives the same text."""
    out, lines = {p}, inserted.splitlines(keepends=True)
    q, block = p, lines[:]
    for _ in range(1000):                            # slide down while the next old line equals the first inserted
        nxt = old[q:old.find("\n", q) + 1 if "\n" in old[q:] else len(old)]
        if not nxt or nxt != block[0]:
            break
        q, block = q + len(nxt), block[1:] + [nxt]
        out.add(q)
    q, block = p, lines[:]
    for _ in range(1000):                            # slide up while the previous old line equals the last inserted
        start = old.rfind("\n", 0, q - 1) + 1 if q > 0 else 0
        prv = old[start:q]
        if not prv or prv != block[-1]:
            break
        q, block = start, [prv] + block[:-1]
        out.add(q)
    return out

def state(path):
    st = load_stamp(path)
    if st is None:
        try:
            Doc(path)
        except UnicodeDecodeError:
            return "unreadable: not UTF-8, so rt can't revise it"
        return "not revised"
    if st["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest():
        changed = []
    else:
        try:
            text = Doc(path).text
        except UnicodeDecodeError:
            return "unreadable: not UTF-8"
        changes = line_diff(st["text"], text)
        changed = [rid for rid, r in st["regions"].items() if edit_spans(changes, r, st["text"], text)]
    changed += [rid for rid, r in st["regions"].items() if r.get("edits") and rid not in changed]
    return f"edited after revising: {', '.join(sorted(changed))}" if changed else "ok"

def cmd_status(_):
    for f in targets():
        print(f"{f.as_posix()}: {state(f)}")

def cmd_baseline(args):
    """Stamp files as they are, without revising them: the named files (replacing their stamps), or else every
    target file that has never been stamped."""
    for f in [pathlib.Path(a) for a in args] or [f for f in targets() if state(f).startswith(("not revised", "unreadable"))]:
        try:
            text = Doc(f).text
        except (OSError, UnicodeDecodeError) as e:
            print(f"skipped {f.as_posix()}: {'not UTF-8' if isinstance(e, UnicodeDecodeError) else e}")
            continue
        fm = FRONTMATTER.match(text)                 # like "whole", everything after the frontmatter
        stamp(f, {"whole": {"status": "baseline", "text": text, "range": [fm.end() if fm else 0, len(text)]}})
        print(f"baseline: {f.as_posix()}")

def cmd_guard(_):                                    # Stop hook: never crashes, never loops
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return                                       # remind once per finish
    try:
        guard()
    except BaseException as e:                       # a broken config or stamp: say so once instead of failing
        if isinstance(e, KeyboardInterrupt):
            raise
        print(json.dumps({"decision": "block", "reason": f"revise-text's finish check couldn't run: {e}. "
                          "Tell the user; finishing again will go through."}))

def guard():
    if project := os.environ.get("CLAUDE_PROJECT_DIR"):
        os.chdir(project)                            # the session may have cd'd into a subfolder
    if not config()["enforce"]:
        return
    pending = [f"{f.as_posix()} ({s})" for f in targets() if (s := state(f)) != "ok"]
    if pending:
        print(json.dumps({"decision": "block",
                          "reason": "Use revise-text on these before finishing: " + "; ".join(pending) +
                                    ". If the user doesn't want a file revised now, run `rt baseline <file>`."}))

def cmd_check(_):
    """One small Pangram check and one StealthGPT rewrite, to confirm keys, route and billing before real runs."""
    if not os.environ.get("STEALTH_KEY"):
        sys.exit("Set STEALTH_KEY in the environment first.")
    cfg = config()
    sample = ("Before you push, run `make test` and keep at most 200 rows in /out/summary.csv, so the report stays "
              "small enough for everyone on the team to read in a minute or two.")
    if os.environ.get("PANGRAM_KEY"):
        print(f"Pangram ({cfg['pangram_model']}): AI score {pangram(sample, cfg['pangram_model'])['pangram_ai']:.2f}")
    else:
        print("Pangram: skipped (PANGRAM_KEY isn't set)")
    print(f"StealthGPT via {cfg['stealth_via']} ({cfg['stealth_model']})")
    if cfg["stealth_via"] == "mcp":
        client = MCP_CLIENTS.setdefault(STEALTH_MCP, Mcp(STEALTH_MCP, os.environ["STEALTH_KEY"]))
        client.start()
        inputs = client.tools.get("humanize_text", {})
        print(f"  humanize_text inputs: {sorted(inputs)}")
        print(f"  token preservation: {'supported' if 'preservation' in inputs else 'not offered; rt still checks every item'}")
    items = exact_items(sample, {})
    guarded, mapping = protect(sample, items)
    raw, spent, billing, _ = (tuple(stealth(guarded, {**VARIANTS[0], "via": cfg["stealth_via"]}, cfg["stealth_model"]))
                              + (None, None))[:4]
    out = restore(INVISIBLE.sub("", raw), mapping).lstrip("\r\n").rstrip()
    lost, problems = item_changes(sample, out, items, ordered=cfg["stealth_via"] == "api")
    print(f"  billed: {spent} words, billing mode {billing!r}")
    print(f"  tokens: {sum(guarded.count(t) for t in mapping)} sent, {sum(raw.count(t) for t in mapping)} came back; "
          + ("protected items intact" if not lost and not problems else
             f"items changed: {lost + problems} (a real run would reject this attempt and try again)"))
    print("  rewritten: " + out)
    if os.environ.get("PANGRAM_KEY"):
        score = pangram(out, cfg["pangram_model"])["pangram_ai"]
        print(f"Pangram on the rewrite: AI score {score:.2f} ("
              + ("would pass" if score < cfg["pass_threshold"] else "would not pass")
              + f" at pass_threshold {cfg['pass_threshold']})")
    if cfg["stealth_via"] == "mcp" and billing in ("prepaid", "payg"):
        print("  NOTE: this run was billed to API credits. To use your subscription, set MCP billing to it in "
              "StealthGPT's usage settings.")

def input_text(args, usage):
    """The text for a one-off API call: --text="...", a file, or stdin (no file, or "-")."""
    inline = next((a.split("=", 1)[1] for a in args if a.startswith("--text=")), None)
    files = [a for a in args if not a.startswith("--")]
    if len(files) + (inline is not None) > 1:
        sys.exit(f"usage: {usage}")
    if inline is not None:
        text = inline
    elif files and files[0] != "-":
        try:
            text = Doc(files[0]).text
        except (OSError, UnicodeDecodeError) as e:
            sys.exit(f"Can't read {files[0]}: {e}")
    else:
        text = sys.stdin.read()
    if not text.strip():
        sys.exit(f"No text given. usage: {usage}")
    return text.strip()

def cmd_pangram(args):
    """Score text with Pangram only."""
    if not os.environ.get("PANGRAM_KEY"):
        sys.exit("Set PANGRAM_KEY in the environment first.")
    cfg, text = config(), input_text(args, 'rt pangram [file | - | --text="..."]')
    res = pangram(text, cfg["pangram_model"])
    print(f"Pangram ({cfg['pangram_model']}, {len(text.split())} words): AI {res['pangram_ai']:.2f}"
          + (f" · AI-assisted {res['pangram_ai_assisted']:.2f}" if res["pangram_ai_assisted"] is not None else "")
          + f" · humanized windows {res['humanized_windows']}"
          + f" · {'pass' if res['pangram_ai'] < cfg['pass_threshold'] else 'no pass'} at {cfg['pass_threshold']}")

def cmd_stealth(args):
    """Rewrite text with StealthGPT only. The rewrite goes to stdout, billing and item checks to stderr.
    Paths, numbers, code and the like are protected as in `rt run`."""
    if not os.environ.get("STEALTH_KEY"):
        sys.exit("Set STEALTH_KEY in the environment first.")
    cfg, text = config(), input_text(args, 'rt stealth [file | - | --text="..."]')
    if (words := len(text.split())) > STEALTH_MAX_WORDS:
        sys.exit(f"The text has {words} words; StealthGPT takes at most {STEALTH_MAX_WORDS}. Split it.")
    items = exact_items(text, {})
    guarded, mapping = protect(text, items)
    raw, spent, billing, _ = (tuple(stealth(guarded, {**VARIANTS[0], "via": cfg["stealth_via"]}, cfg["stealth_model"]))
                              + (None, None))[:4]
    keep = set(INVISIBLE.findall(text))
    out = restore(INVISIBLE.sub(lambda m: m[0] if m[0] in keep else "", raw.replace("\r\n", "\n")), mapping).strip()
    print(f"StealthGPT via {cfg['stealth_via']} ({cfg['stealth_model']}): billed {spent} words, "
          f"billing mode {billing!r}, length ratio {len(out.split()) / words:.2f}", file=sys.stderr)
    if items:
        lost, problems = item_changes(text, out, items, ordered=cfg["stealth_via"] == "api")
        if lost or problems:
            print(f"WARNING: protected items changed: {lost + problems}", file=sys.stderr)
    print(out)

COMMANDS = {"run": cmd_run, "check": cmd_check, "retry": cmd_retry, "verify": cmd_verify, "backup": cmd_backup,
            "status": cmd_status, "baseline": cmd_baseline, "guard": cmd_guard,
            "pangram": cmd_pangram, "stealth": cmd_stealth}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit("usage: rt.py {" + ",".join(COMMANDS) + "} ...")
    COMMANDS[sys.argv[1]](sys.argv[2:])
