"""Summarize ~/.revise-text/history*.jsonl for /revise-text:insights.

  python3 insights.py [--since YYYY-MM-DD] [--export-cases[=DIR]]

Prints JSON numbers that map to the findings table in the README. --export-cases writes REVIEW, ERROR and
mismatched regions (where text was logged) as regression cases.
"""
import collections, json, os, pathlib, re, sys

HOME = pathlib.Path(os.path.expanduser("~/.revise-text"))
PLUGIN = pathlib.Path(__file__).resolve().parent.parent.parent.parent
KINDS = [("code", r"^`"), ("url", r"^https?://"), ("path", r"^/"), ("number", r"^\d"), ("heading", r"^#"),
         ("table", r"^\|")]

def arg(name, default=None):                        # --name=value, --name value, or a bare --name (True)
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name:
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            return nxt if nxt and not nxt.startswith("--") else True
    return default

def read_history(home):                              # skips anything that isn't a well-formed record
    records = []
    for f in sorted(home.glob("history*.jsonl")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            try:
                r = clean(json.loads(line)) if line.strip() else None
            except ValueError:
                r = None
            if r:
                records.append(r)
            elif line.strip():
                print(f"skipped {f.name} line {n}: not a history record", file=sys.stderr)
    return records

def clean(r):
    """The record with every field the readers use present and of the right type, or None."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    text = lambda v, d: v if isinstance(v, str) else d
    strs = lambda v: [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    if not isinstance(r, dict) or r.get("type") not in ("run", "verify") or not isinstance(r.get("run_id"), str) \
            or not re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", text(r.get("ts"), "")):
        return None
    if r["type"] == "verify":
        return {**r, "verified": r.get("verified") is True, "final": r.get("final") is True,
                "mismatches": strs(r.get("mismatches"))}
    steps = []
    for st in r.get("steps") if isinstance(r.get("steps"), list) else []:
        if isinstance(st, dict) and isinstance(st.get("step"), str):
            steps.append({k: v for k, v in st.items() if num(v) or (isinstance(v, str) and
                          k in ("step", "rejected", "billing", "via", "run_id", "error"))})
            steps[-1].update(missing=strs(st.get("missing")), **({"unexpected": strs(st["unexpected"])}
                                                                  if "unexpected" in st else {}))
    out = {**r, "steps": steps, "mode": text(r.get("mode"), "inline"), "source": text(r.get("source"), "?"),
           "region": text(r.get("region"), "?"), "result": text(r.get("result"), "ERROR"),
           "plugin_version": text(r.get("plugin_version"), "unknown"),
           "humanize_writing_commit": text(r.get("humanize_writing_commit"), "unknown"),
           "critique": strs(r.get("critique")), "requirement_list": strs(r.get("requirement_list")),
           "exact_list": strs(r.get("exact_list"))}
    for k in ("stealth_cost_usd", "pangram_checks", "input_words", "exact_items", "stealth_words_billed",
              "stealth_subscription_words", "stealth_unknown_billing_words"):
        out[k] = r[k] if num(r.get(k)) else 0
    for k in ("best_pangram_ai", "pass_threshold"):
        out[k] = r[k] if num(r.get(k)) else None
    for k in ("passed_at", "retry_of", "error", "text_before", "text_after", "select", "spec"):
        if not isinstance(r.get(k), str):
            out.pop(k, None)
    if not (isinstance(r.get("word_range"), list) and len(r["word_range"]) == 2 and all(map(num, r["word_range"]))):
        out["word_range"] = None
    return out

def load(since):
    records = read_history(HOME)
    runs = [r for r in records if r["type"] == "run" and r["ts"][:10] >= since]
    verifies = collections.defaultdict(list)
    for r in records:                                # older versions logged "not verified" for regions with no verdict
        if r["type"] == "verify" and (r["verified"] or r["final"] or r["mismatches"] != ["not verified"]):
            verifies[r["run_id"]].append(r)
    return runs, verifies

def kind(item):
    return next((k for k, p in KINDS if re.match(p, item)), "other")

CHANGES = [("added item", r"^added "), ("sign or suffix next to an item", r"became"),
           ("block moved off its line", r"no longer on its own line"), ("blank line lost", r"lost a blank line"),
           ("items reordered", r"reordered"), ("moved across sections", r"another section|changed order"),
           ("invented token", r"^\[\[")]

def change(reason):                                  # what kind of change an "unexpected" entry describes
    return next((k for k, p in CHANGES if re.search(p, reason)), "other")

def added_item(reason):                              # 'added "200"' (older records: "added '200'")
    try:
        return json.loads(reason[6:])
    except ValueError:
        return reason[7:-1]

def rate(n, d):
    return None if not d else round(n / d, 3)

def summarize(runs, verifies):
    first = [r for r in runs if r["mode"] != "retry"]
    steps = [s for r in runs for s in r["steps"]]
    draft = [(s, r.get("pass_threshold") or 0.5) for r in runs for s in r["steps"]
             if s["step"] == "draft" and "pangram_ai" in s]
    pass_t = lambda r: r["result"] == "PASS"
    # attempt n reached / passed (retries are counted separately)
    attempts = collections.Counter(); passes = collections.Counter()
    for r in first:
        for s in r["steps"]:
            if s["step"].startswith("stealth_"):
                attempts[s["step"]] += 1
        if r.get("passed_at"):
            passes[r["passed_at"]] += 1
    retries = [r for r in runs if r["mode"] == "retry"]
    rejected = [s for s in steps if s.get("rejected")]
    lost = collections.Counter(kind(i) for s in rejected for i in s.get("missing", []))
    changes = collections.Counter(change(u) for s in rejected for u in s.get("unexpected", []))
    added = collections.Counter(kind(added_item(u)) for s in rejected for u in s.get("unexpected", []) if u.startswith("added "))
    ratios = sorted(s["length_ratio"] for s in steps if "length_ratio" in s)
    # verification: the first verify record of each run is the first try
    v_first = [verifies[r["run_id"]][0] for r in first if verifies.get(r["run_id"])]
    retry_verified = [verifies[r["run_id"]][-1]["verified"] for r in retries if verifies.get(r["run_id"])]
    finals = sum(any(v["final"] and not v["verified"] for v in verifies.get(r["run_id"], [])) for r in runs)
    ids = {r["run_id"] for r in runs}
    mismatches = [m for rid, vs in verifies.items() if rid in ids for v in vs for m in v["mismatches"]
                  if m != "not verified"]
    critique = collections.Counter(c.lower() for r in first for c in r.get("critique", []))
    by_version = collections.defaultdict(lambda: [0, 0])
    by_week = collections.defaultdict(lambda: [0, 0])
    for r in first:
        for bucket in (by_version[f'{r["plugin_version"]} / {r["humanize_writing_commit"][:7]}'],
                       by_week[r["ts"][:10]]):
            bucket[0] += pass_t(r); bucket[1] += 1
    humanized = [s for s in steps if s["step"] != "draft" and "humanized_windows" in s]
    return {
        "regions": len(first), "retries": len(retries),
        "results": collections.Counter(r["result"] for r in first),
        "passed_at": dict(passes),
        "draft_pass_rate": rate(sum(s["pangram_ai"] < t for s, t in draft), len(draft)),
        "stealth_attempt_pass_rate": {k: rate(passes[k], attempts[k]) for k in sorted(attempts)},
        "retry_pass_rate": rate(sum(pass_t(r) for r in retries), len(retries)),
        "rejections": collections.Counter(s["rejected"] for s in rejected),
        "lost_item_kinds": dict(lost), "added_item_kinds": dict(added), "item_change_kinds": dict(changes),
        "length_ratio": {"min": ratios[0], "median": ratios[len(ratios) // 2], "max": ratios[-1]} if ratios else None,
        "short_share": rate(sum(r["result"] == "SHORT" for r in first), len(first)),
        "verified_first_try": rate(sum(v["verified"] for v in v_first), len(v_first)),
        "retry_fixed_mismatch": rate(sum(retry_verified), len(retry_verified)),
        "kept_draft_with_final": finals,
        "mismatches": mismatches[-30:],
        "top_critique_points": critique.most_common(15),
        "pass_rate_by_version": {k: rate(*v) for k, v in by_version.items()},
        "pass_rate_by_day": {k: rate(*v) for k, v in sorted(by_week.items())},
        "pangram_humanized_flag_share": rate(sum(s["humanized_windows"] > 0 for s in humanized), len(humanized)),
        "stealth_cost_usd": round(sum(r["stealth_cost_usd"] for r in runs), 2),
        "stealth_subscription_words": sum(r["stealth_subscription_words"] for r in runs),
        "billing_modes": collections.Counter(st.get("billing", "unknown") for st in steps if "words_billed" in st),
        "pangram_checks": sum(r["pangram_checks"] for r in runs),
    }

def export(runs, verifies, dest):
    dest.mkdir(parents=True, exist_ok=True)
    n, seen = 0, set()
    for r in runs:
        vs = verifies.get(r["run_id"], [])
        mismatched = any(not v["verified"] and v["mismatches"] != ["not verified"] for v in vs)
        if "text_before" not in r or not (r["result"] in ("REVIEW", "ERROR") or mismatched) \
                or (r["source"], r["region"], r["text_before"]) in seen:   # a retry repeats its region's draft
            continue
        seen.add((r["source"], r["region"], r["text_before"]))
        try:                                         # older records only count requirements; read them from the spec
            spec = json.loads(pathlib.Path(r["spec"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            spec = {}
        case = {"id": r["run_id"], "source": r["source"], "region": r["region"], "draft": r["text_before"],
                "requirements": r.get("requirement_list") or spec.get("requirements", []),
                "exact": r.get("exact_list") or spec.get("exact", []),
                "word_range": r.get("word_range") or (spec.get("word_range") if len(spec.get("regions", [])) == 1
                                                      else None), "why": r["result"] if not mismatched else "mismatch",
                "mismatches": [m for v in vs for m in v["mismatches"]]}
        (dest / f"{r['run_id']}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
        n += 1
    print(f"exported {n} cases to {dest}", file=sys.stderr)

if __name__ == "__main__":
    since = arg("--since", "0000")
    if since is True:
        sys.exit("--since needs a date, e.g. --since 2026-10-01")
    runs, verifies = load(since)
    print(json.dumps(summarize(runs, verifies), indent=2, ensure_ascii=False))
    if out := arg("--export-cases"):
        default = PLUGIN / "tests" / "cases" if (PLUGIN / ".git").exists() else pathlib.Path("revise-text-cases")
        export(runs, verifies, default if out is True else pathlib.Path(out))
