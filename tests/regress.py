"""Regression test: run every case in tests/cases through the real APIs and summarize.

  python3 tests/regress.py [--cases=DIR] [--redraft | --plain] [--judge] [--out=results.json]
  python3 tests/regress.py --compare=old.json,new.json

A case is a JSON file: {"id", "draft"?, "requirements", "exact"?, "word_range"?, "human"?: true}. A case without a
draft needs --redraft or --plain.
--redraft  writes a fresh draft from the requirements with `claude -p` and this checkout's writing rules, so two
           checkouts (old and new humanize-writing) can be compared. Human-written controls are never redrafted.
--plain    the baseline: a plain draft rewritten with every pass of humanize-writing, then one Pangram check and
           no StealthGPT. Compare its results with a normal run to see what the pipeline adds.
--judge    asks `claude -p` whether the final text still meets every requirement (the meaning check).
Costs real Pangram checks and StealthGPT words.
"""
import json, pathlib, statistics, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
REFS = ROOT / "skills" / "revise-text" / "references"
sys.path.insert(0, str(ROOT / "skills" / "revise-text" / "scripts"))
import rt

def arg(name, default=None):                        # --name=value, --name value, or a bare --name (True)
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name:
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            return nxt if nxt and not nxt.startswith("--") else True
    return default

def claude(prompt):
    return subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, check=True).stdout.strip()

def redraft(case):
    rules = (REFS / "humanize-writing" / "ai-tells.md").read_text() + "\n\n" + (REFS / "overrides.md").read_text()
    lo, hi = case.get("word_range") or (150, 250)
    return claude(f"Follow these writing rules:\n\n{rules}\n\nWrite one Markdown passage of {lo}-{hi} words that "
                  "conveys exactly these requirements, with every string in backticks kept verbatim. Output only "
                  "the passage.\n\n" + "\n".join(case["requirements"]))

def plain(case):
    rules = (REFS / "humanize-writing" / "humanize-writing.md").read_text()
    lo, hi = case.get("word_range") or (150, 250)
    draft = case.get("draft") or claude(f"Write one Markdown passage of {lo}-{hi} words that conveys exactly these "
                                        "requirements. Output only the passage.\n\n" + "\n".join(case["requirements"]))
    return claude(f"{rules}\n\nRewrite the text below using every pass above. Keep every string in backticks verbatim. "
                  f"Output only the rewritten text, without a Changes table.\n\n{draft}")

def judge(case, text):
    answer = claude("Does the TEXT convey every REQUIREMENT exactly, including limits and conditions? Reply with "
                    "only `ok`, or the first mismatch as `R<n>: what changed`.\n\nREQUIREMENTS:\n"
                    + "\n".join(case["requirements"]) + f"\n\nTEXT:\n{text}")
    return answer == "ok", answer

def run_case(case, cfg):
    if case.get("human"):
        ai = rt.pangram(case["draft"], cfg["pangram_model"])["pangram_ai"]
        return {"id": case["id"], "human": True, "pangram_ai": ai, "flagged": ai >= cfg["pass_threshold"]}
    if arg("--plain"):
        text = plain(case)
        ai = rt.pangram(text, cfg["pangram_model"])["pangram_ai"]
        res = {"status": "PASS" if ai < cfg["pass_threshold"] else "REVIEW", "out": text, "best_ai": ai,
               "steps": [{"step": "draft", "pangram_ai": ai}]}
        res["passed_at"] = "draft" if res["status"] == "PASS" else None
    else:
        draft = redraft(case) if arg("--redraft") else case["draft"]
        res = rt.safe_humanize(draft, rt.exact_items(draft, case), cfg, 1, case.get("word_range"))
    stealth = [s for s in res["steps"] if s["step"].startswith("stealth")]
    row = {"id": case["id"], "result": res["status"], "passed_at": res.get("passed_at"),
           "draft_ai": next((s["pangram_ai"] for s in res["steps"] if s["step"] == "draft"), None),
           "final_ai": res.get("best_ai"), "attempts": len(stealth),
           "intact": sum(s.get("rejected") != "exact items" for s in stealth),
           "length_ratios": [s["length_ratio"] for s in stealth], "error": res.get("error")}
    if arg("--judge"):
        row["verified"], row["judge"] = judge(case, res["out"])
    return row

def summary(rows):
    gen = [r for r in rows if not r.get("human")]
    human = [r for r in rows if r.get("human")]
    ratios = [x for r in gen for x in r["length_ratios"]]
    pct = lambda n, d: round(n / d, 3) if d else None
    return {"cases": len(gen), "human_controls": len(human),
            "pass_rate": pct(sum(r["result"] == "PASS" for r in gen), len(gen)),
            "exact_items_intact": pct(sum(r["intact"] for r in gen), sum(r["attempts"] for r in gen)),
            "verified": pct(sum(r.get("verified", False) for r in gen), len(gen)) if arg("--judge") else None,
            "median_length_ratio": statistics.median(ratios) if ratios else None,
            "human_flagged_as_ai": pct(sum(r["flagged"] for r in human), len(human)),
            "errors": sum(r["result"] == "ERROR" for r in gen)}

def compare(old_path, new_path):
    old, new = (json.loads(pathlib.Path(p).read_text())["summary"] for p in (old_path, new_path))
    worse = []
    for key, higher_is_better in [("pass_rate", True), ("exact_items_intact", True), ("verified", True),
                                  ("human_flagged_as_ai", False)]:
        o, n = old.get(key), new.get(key)
        print(f"{key:22} {o!s:>8} → {n!s:>8}")
        if o is not None and n is not None and (n < o - 0.05 if higher_is_better else n > o + 0.05):
            worse.append(key)
    print("WORSE on: " + ", ".join(worse) if worse else "not worse")
    sys.exit(1 if worse else 0)

if __name__ == "__main__":
    if pair := arg("--compare"):
        compare(*pair.split(","))
    rt.need_keys()
    cfg = rt.config()
    cases = [json.loads(p.read_text()) for p in sorted(pathlib.Path(arg("--cases", ROOT / "tests" / "cases")).glob("*.json"))]
    if not cases:
        sys.exit("no cases; add some to tests/cases or export them with /revise-text:insights --export-cases")
    if not (arg("--redraft") or arg("--plain")) and (no := [c["id"] for c in cases if not c.get("draft")]):
        sys.exit(f"cases without a draft need --redraft or --plain: {no}")
    rows = []
    for case in cases:
        rows.append(run_case(case, cfg))
        print(json.dumps(rows[-1], ensure_ascii=False))
    result = {"versions": rt.versions(), "mode": "plain" if arg("--plain") else "redraft" if arg("--redraft") else "drafts", "summary": summary(rows), "rows": rows}
    print(json.dumps(result["summary"], indent=2))
    pathlib.Path(arg("--out", "regress-results.json")).write_text(json.dumps(result, indent=2, ensure_ascii=False))
