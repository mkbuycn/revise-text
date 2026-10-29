"""Tests for rt.py that need no API keys. Run: python3 -m unittest discover tests"""
import concurrent.futures as cf, contextlib, io, json, os, pathlib, sys, tempfile, unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "skills" / "revise-text" / "scripts"))
import rt

INSTRUCTION = """---
title: summarize logs
---
## Task

The logs are in /data/logs. Write `/out/summary.csv` with columns `ts`, `level`, `count`.
Count only ERROR and WARN lines, at most 200 rows, newest first. Version 1 of the format uses R1.

```bash
# not a heading
cat /data/logs/app.log
```

## Notes

<!-- revise:start id=tip -->
Keep the file small.
<!-- revise:end -->
Untouched trailing line.
"""

class Project(unittest.TestCase):
    """Each test runs in an empty project folder with its own history folder."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.home = mock.patch.object(rt, "HOME", pathlib.Path(self.tmp.name) / "home")
        self.home.start()
        self.keys = mock.patch.dict(os.environ, {"PANGRAM_KEY": "p", "STEALTH_KEY": "s"})
        self.keys.start()

    def tearDown(self):
        self.keys.stop()
        self.home.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def write(self, path, text, crlf=False, spec=None, cfg=None):
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode())
        if spec is not None:
            rt.spec_for(path).parent.mkdir(parents=True, exist_ok=True)
            rt.spec_for(path).write_text(json.dumps(spec))
        if cfg is not None:
            (rt.ROOT / "config.json").parent.mkdir(parents=True, exist_ok=True)
            (rt.ROOT / "config.json").write_text(json.dumps(cfg))
        return p

    def rt(self, *args, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            rt.COMMANDS[args[0]](list(args[1:]))
        return out.getvalue()

    def history(self):
        return [json.loads(l) for l in (rt.HOME / "history.jsonl").read_text().splitlines()]


class Regions(unittest.TestCase):
    def test_whole_skips_frontmatter(self):
        a, b, *_ = rt.spans(INSTRUCTION, {"regions": [{"id": "w", "select": "whole"}]})["w"]
        self.assertTrue(INSTRUCTION[a:].startswith("## Task"))
        self.assertEqual(b, len(INSTRUCTION))

    def test_heading_ignores_comment_in_code_fence(self):
        a, b, *_ = rt.spans(INSTRUCTION, {"regions": [{"id": "t", "select": "## Task"}]})["t"]
        body = INSTRUCTION[a:b]
        self.assertTrue(body.startswith("\nThe logs"))
        self.assertIn("cat /data/logs/app.log", body)
        self.assertNotIn("## Notes", body)

    def test_marker(self):
        a, b, *_ = rt.spans(INSTRUCTION, {"regions": [{"id": "tip", "select": "marker"}]})["tip"]
        self.assertEqual(INSTRUCTION[a:b], "Keep the file small.")

    def test_missing_heading_exits(self):
        with self.assertRaises(SystemExit):
            rt.spans(INSTRUCTION, {"regions": [{"id": "x", "select": "## Nope"}]})


class ExactItems(unittest.TestCase):
    spec = {"requirements": ["R3: count only `ERROR` and `WARN` lines"], "exact": ["newest first"]}

    def test_collects_patterns_and_spec(self):
        items = rt.exact_items(INSTRUCTION, self.spec)
        for i in ["/data/logs", "`/out/summary.csv`", "200", "ERROR", "WARN", "newest first", "## Task",
                  "1"]:
            self.assertIn(i, items)
        self.assertTrue(any(i.startswith("```bash") for i in items))
        self.assertNotIn("/data/logs.", items)

    def test_protect_restore_round_trip(self):
        items = rt.exact_items(INSTRUCTION, self.spec)
        guarded, mapping = rt.protect(INSTRUCTION, items)
        self.assertNotIn("/data/logs", guarded)
        self.assertEqual(rt.restore(guarded, mapping), INSTRUCTION)

    def test_number_item_does_not_touch_tokens_or_words(self):
        guarded, mapping = rt.protect("R1 uses 1 and 12 items plus 3", ["12", "1", "3"])
        self.assertIn("R1", guarded)
        self.assertEqual(len(mapping), 3)
        self.assertEqual(rt.restore(guarded, mapping), "R1 uses 1 and 12 items plus 3")


def fake_stealth(rewrite):
    """A StealthGPT stand-in that keeps [[E…]] tokens and applies `rewrite` to the text."""
    return lambda text, variant, model: (rewrite(text), None)


class Humanize(Project):
    cfg = {**rt.DEFAULTS, "min_words": 5}
    draft = "\n\nThe logs live in /data/logs and the report keeps at most 200 rows, newest first please.\n\n"

    def test_short(self):
        self.assertEqual(rt.humanize("too short", [], self.cfg)["status"], "SHORT")

    def test_draft_passes(self):
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}):
            res = rt.humanize(self.draft, rt.exact_items(self.draft, {}), self.cfg)
        self.assertEqual((res["status"], res["passed_at"], res["out"]), ("PASS", "draft", self.draft))

    def test_stealth_pass_keeps_whitespace_and_items(self):
        scores = iter([{"pangram_ai": 0.9}, {"pangram_ai": 0.05}])
        with mock.patch.object(rt, "pangram", side_effect=lambda *a: next(scores)), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("live in", "sit in") + "​")):
            res = rt.humanize(self.draft, rt.exact_items(self.draft, {}), self.cfg)
        self.assertEqual(res["passed_at"], "stealth_1")
        self.assertTrue(res["out"].startswith("\n\nThe logs sit in /data/logs"))
        self.assertTrue(res["out"].endswith("please.\n\n"))
        self.assertNotIn("​", res["out"])

    def test_lost_item_rejected_then_review(self):
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.9}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("[[E0]]", "two hundred"))):
            res = rt.humanize(self.draft, ["200"], self.cfg)
        self.assertEqual(res["status"], "REVIEW")
        self.assertEqual([s.get("rejected") for s in res["steps"][1:]], ["exact items", "exact items"])
        self.assertEqual(res["out"], self.draft)

    def test_length_rejected(self):
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.9}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + " padding" * 40)):
            res = rt.humanize(self.draft, [], self.cfg)
        self.assertEqual(res["steps"][1]["rejected"], "length")

    def test_error_keeps_draft(self):
        with mock.patch.object(rt, "pangram", side_effect=RuntimeError("down")):
            res = rt.safe_humanize(self.draft, [], self.cfg)
        self.assertEqual((res["status"], res["out"]), ("ERROR", self.draft))


SPEC = {"regions": [{"id": "task", "select": "## Task"}, {"id": "tip", "select": "marker"}],
        "requirements": ["R1: the logs are in `/data/logs`", "R4: at most 200 rows, newest first"]}
CFG = {"targets": ["tasks/*/instruction.md"], "enforce": True, "min_words": 3, "check_draft": False}


class Flow(Project):
    def run_all(self, crlf=False):
        path = self.write("tasks/001/instruction.md", INSTRUCTION, crlf=crlf, spec=SPEC, cfg=CFG)
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.9}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("Count only", "Only count")
                                                                    .replace("Keep", "Please keep"))):
            out = self.rt("run", "tasks/001/instruction.md", "--critique=tidy summary; three-part list")
        return path, out

    def test_run_then_verify_changes_only_regions(self):
        path, out = self.run_all()
        self.assertIn("Verify: rt verify tasks/001/instruction.md task=ok tip=ok", out)
        self.assertEqual(path.read_text(), INSTRUCTION)            # nothing applied before verify
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("Count only", "Only count")
                                                                    .replace("Keep", "Please keep"))):
            self.rt("run", "tasks/001/instruction.md")
        self.rt("verify", "tasks/001/instruction.md", "task=ok", "tip=ok")
        expected = (INSTRUCTION.replace("Count only", "Only count").replace("Keep", "Please keep")
                    .replace("<!-- revise:start id=tip -->\n", "").replace("<!-- revise:end -->\n", "\n"))
        self.assertEqual(path.read_text(), expected)
        self.assertEqual(rt.state(path), "ok")
        runs = [r for r in self.history() if r["type"] == "run"]
        self.assertEqual(runs[0]["critique"], ["tidy summary", "three-part list"])
        self.assertTrue(list((rt.ROOT / "backups").iterdir()))

    def test_crlf_preserved(self):
        path, _ = self.run_all(crlf=True)
        self.rt("verify", "tasks/001/instruction.md", "task=ok", "tip=ok", "--final")
        raw = path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"Untouched trailing line.\r\n", raw)

    def test_mismatch_not_applied_then_final_keeps_draft(self):
        path, _ = self.run_all()
        out = self.rt("verify", "tasks/001/instruction.md", "task=R4: lost newest first", "tip=ok")
        self.assertIn("Not applied", out)
        self.assertEqual(path.read_text(), INSTRUCTION)
        out = self.rt("verify", "tasks/001/instruction.md", "task=R4: lost newest first", "tip=ok", "--final")
        self.assertIn("task: REVIEW", out)
        text = path.read_text()
        self.assertIn("Count only ERROR", text)                    # draft kept
        self.assertIn("Please keep the file small.", text)         # verified region applied
        verifies = [r for r in self.history() if r["type"] == "verify"]
        self.assertEqual(verifies[-2]["mismatches"], ["R4: lost newest first"])

    def test_retry_limited(self):
        self.run_all()
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.2}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t)):
            out = self.rt("retry", "tasks/001/instruction.md", "task")
            self.assertIn("PASS at stealth_3", out)
            with self.assertRaises(SystemExit):
                self.rt("retry", "tasks/001/instruction.md", "task")
        retry = self.history()[-1]
        self.assertEqual((retry["mode"], retry["region"]), ("retry", "task"))
        self.assertIn("retry_of", retry)

    def test_verify_refuses_if_file_changed(self):
        path, _ = self.run_all()
        path.write_text(INSTRUCTION.replace("Count only", "Count just"))
        with self.assertRaises(SystemExit):
            self.rt("verify", "tasks/001/instruction.md", "task=ok", "tip=ok")

    def test_status_guard_baseline(self):
        self.write("tasks/002/instruction.md", "old file\n", cfg=CFG)
        block = json.loads(self.rt("guard", stdin="{}"))
        self.assertEqual(block["decision"], "block")
        self.assertEqual(self.rt("guard", stdin='{"stop_hook_active": true}'), "")
        self.rt("baseline")
        self.assertEqual(self.rt("guard", stdin="{}"), "")
        self.assertIn("tasks/002/instruction.md: ok", self.rt("status"))
        pathlib.Path("tasks/002/instruction.md").write_text("hand edited\n")
        self.assertIn("edited after revising", self.rt("status"))

    def test_guard_off_without_enforce(self):
        self.write("tasks/003/instruction.md", "x\n", cfg={**CFG, "enforce": False})
        self.assertEqual(self.rt("guard", stdin="{}"), "")

    def test_missing_keys(self):
        self.write("tasks/001/instruction.md", INSTRUCTION, spec=SPEC, cfg=CFG)
        with mock.patch.dict(os.environ, {"PANGRAM_KEY": ""}), self.assertRaises(SystemExit):
            self.rt("run", "tasks/001/instruction.md")

    def test_pangram_command_inline_file_stdin(self):
        self.write("note.md", "﻿From a file.\r\n")
        seen = []
        score = lambda text, model: seen.append(text) or {"pangram_ai": 0.7, "pangram_ai_assisted": None,
                                                          "humanized_windows": 0}
        with mock.patch.object(rt, "pangram", score):
            out = self.rt("pangram", "--text=Inline words here")
            self.rt("pangram", "note.md")
            self.rt("pangram", "-", stdin="From stdin\n")
        self.assertEqual(seen, ["Inline words here", "From a file.", "From stdin"])
        self.assertIn("AI 0.70", out)
        self.assertIn("no pass at 0.5", out)
        with self.assertRaises(SystemExit):
            self.rt("pangram", "--text=   ")
        with self.assertRaises(SystemExit):
            self.rt("pangram", "note.md", "--text=both")

    def test_stealth_command_protects_items(self):
        sent = []
        def fake(text, variant, model):
            sent.append(text)
            return text.replace("Keep", "Hold"), 20, "subscription"
        err = io.StringIO()
        with mock.patch.object(rt, "stealth", fake), contextlib.redirect_stderr(err):
            out = self.rt("stealth", "--text=Keep at most 200 rows in /out/a.csv.")
        self.assertEqual(out, "Hold at most 200 rows in /out/a.csv.\n")
        self.assertNotIn("200", sent[0])
        self.assertIn("billed 20 words", err.getvalue())
        self.assertNotIn("WARNING", err.getvalue())
        with mock.patch.dict(os.environ, {"STEALTH_KEY": ""}), self.assertRaises(SystemExit):
            self.rt("stealth", "--text=x")



class ReviewFindings(Project):
    """Regression tests for defects found in review."""
    cfg = {**rt.DEFAULTS, "min_words": 1, "check_draft": False}

    def flow(self, text, spec, rewrite=lambda t: t.replace("alpha", "ALPHA"), verdict="ok", raw=False):
        path = pathlib.Path("doc.md")
        path.write_bytes(text if raw else text.encode())
        rt.spec_for("doc.md").parent.mkdir(parents=True, exist_ok=True)
        rt.spec_for("doc.md").write_text(json.dumps(spec))
        (rt.ROOT / "config.json").write_text(json.dumps({"min_words": 1, "check_draft": False}))
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(rewrite)):
            self.rt("run", "doc.md")
        self.rt("verify", "doc.md", *[f"{r['id']}={verdict}" for r in spec["regions"]])
        return path.read_bytes()

    def test_overlapping_regions_refused(self):
        text = "## Notes\n\nalpha\n<!-- revise:start id=m -->\nalpha inside\n<!-- revise:end -->\nmore\n"
        for regions in ([{"id": "n", "select": "## Notes"}, {"id": "m", "select": "marker"}],
                        [{"id": "w", "select": "whole"}, {"id": "m", "select": "marker"}],
                        [{"id": "a", "select": "## Notes"}, {"id": "b", "select": "## Notes"}]):
            with self.assertRaises(SystemExit):
                rt.spans(text, {"regions": regions})

    def test_bom_and_frontmatter_untouched(self):
        for fm in ("---\ntitle: alpha\n---\n", "---\n---\n", "---\ntitle: alpha\n---"):
            text = "﻿" + fm + ("\n" if not fm.endswith("\n") else "") + "alpha body\n"
            out = self.flow(text, {"regions": [{"id": "w", "select": "whole"}]}).decode()
            self.assertEqual(out, text.replace("alpha body", "ALPHA body"), fm)

    def test_bom_heading_on_first_line(self):
        out = self.flow("﻿# Title\nalpha\n", {"regions": [{"id": "t", "select": "# Title"}]}).decode()
        self.assertEqual(out, "﻿# Title\nALPHA\n")

    def test_mixed_line_endings_outside_regions_kept(self):
        text = b"line kept\n## Mid\r\nalpha one\r\nalpha two\r\n## End\nunix line\n"
        out = self.flow(text, {"regions": [{"id": "m", "select": "## Mid"}]}, raw=True)
        self.assertEqual(out, text.replace(b"alpha", b"ALPHA"))

    def test_markers_in_code_fence_and_unlisted_blocks_kept(self):
        text = ("## Intro\nalpha\n## Usage\n```md\n<!-- revise:start id=ex -->\ntext\n<!-- revise:end -->\n```\n"
                "<!-- revise:start id=other -->\nalpha other\n<!-- revise:end -->\n")
        out = self.flow(text, {"regions": [{"id": "i", "select": "## Intro"}]}).decode()
        self.assertEqual(out, text.replace("## Intro\nalpha", "## Intro\nALPHA"))

    def test_tilde_fences(self):
        text = "~~~markdown\n## Setup\nexample\n~~~\n\n## Setup\nalpha real\n~~~bash\n# comment\nrun alpha --beta\n~~~\nalpha after\n## Next\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertTrue(text[a:b].startswith("alpha real") and text[a:b].endswith("alpha after\n"))
        guarded, _ = rt.protect(text[a:b], rt.exact_items(text[a:b], {}))
        self.assertNotIn("run alpha", guarded)

    def test_marker_edge_cases(self):
        empty = "<!-- revise:start id=x -->\n<!-- revise:end -->\n\nalpha outside\n<!-- revise:start id=y -->\nbeta\n<!-- revise:end -->\n"
        blocks = rt.markers(empty)
        self.assertEqual(empty[blocks["x"][0]:blocks["x"][1]], "")
        self.assertEqual(empty[blocks["y"][0]:blocks["y"][1]], "beta")
        nested = "<!-- revise:start id=a -->\n<!-- revise:start id=b -->\ng\n<!-- revise:end -->\n<!-- revise:end -->\n"
        for bad in (nested, "<!-- revise:end -->\n", "<!-- revise:start id=a -->\nx\n"):
            with self.assertRaises(ValueError):
                rt.markers(bad)
            with self.assertRaises(SystemExit):
                rt.spans(bad, {"regions": [{"id": "a", "select": "marker"}]})

    def test_invisible_chars_inside_items_survive(self):
        draft = "keep `a‍b` and the row below intact for the reader please\n| x­y |\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + "​")):
            res = rt.humanize(draft, rt.exact_items(draft, {}), self.cfg)
        self.assertIn("`a‍b`", res["out"])
        self.assertIn("| x­y |", res["out"])

    def test_overlapping_items_are_protected_whole(self):
        guarded, mapping = rt.protect("use version 2.5.1 now", ["version 2", "2.5.1"])
        self.assertEqual(guarded, "use [[E0]] now")
        self.assertEqual(mapping, {"[[E0]]": "version 2.5.1"})

    def test_token_collisions(self):
        guarded, mapping = rt.protect("template [[E0]] and /srv/data", ["/srv/data"])
        self.assertEqual(rt.restore(guarded, mapping), "template [[E0]] and /srv/data")
        draft = "the data lives in /srv/data and nowhere else at all\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + " see [[E7]]")):
            res = rt.humanize(draft, rt.exact_items(draft, {}), {**self.cfg, "length_ratio": [0.5, 2]})
        self.assertEqual(res["status"], "REVIEW")
        self.assertEqual(res["steps"][0]["rejected"], "exact items")
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + " [[E0]]")):
            res = rt.humanize(draft, rt.exact_items(draft, {}), {**self.cfg, "length_ratio": [0.5, 2]})
        self.assertIn("unexpected", res["steps"][0])

    def test_error_keeps_billed_steps(self):
        draft = "alpha beta gamma delta epsilon\n"
        with mock.patch.object(rt, "pangram", side_effect=RuntimeError("down")), \
             mock.patch.object(rt, "stealth", lambda t, v, m: (t, 123)):
            res = rt.safe_humanize(draft, [], self.cfg)
        self.assertEqual(res["status"], "ERROR")
        self.assertEqual(res["steps"][0]["words_billed"], 123)

    def test_word_range(self):
        draft = " ".join(["word"] * 10) + "\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + " more")):
            res = rt.humanize(draft, [], {**self.cfg, "length_ratio": [0.5, 2]}, word_range=[8, 10])
        self.assertEqual(res["steps"][0]["rejected"], "length")
        self.assertEqual(rt.region_range({"regions": [{"id": "a", "select": "whole"}], "word_range": [1, 2]}, "a"), [1, 2])
        two = {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B", "word_range": [3, 4]}],
               "word_range": [1, 2]}
        self.assertIsNone(rt.region_range(two, "a"))
        self.assertEqual(rt.region_range(two, "b"), [3, 4])

    def test_targets_skip_own_files(self):
        self.write("notes/a.md", "x\n", cfg={"targets": ["**/*.md"], "enforce": True})
        self.write(".revise-text/backups/k-1.md", "x\n")
        self.write(".revise-text/paste/1.md", "x\n")
        self.assertEqual([f.as_posix() for f in rt.targets()], ["notes/a.md"])

    def test_guard_uses_project_dir(self):
        self.write("tasks/001/instruction.md", "x\n", cfg=CFG)
        pathlib.Path("sub").mkdir()
        os.chdir("sub")
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.tmp.name}):
            self.assertIn("block", self.rt("guard", stdin="{}"))

    def test_partial_verify_logs_only_verdicts(self):
        text = "## A\nalpha one\n## B\nalpha two\n"
        self.flow(text, {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B"}]}, verdict="ok")
        before = len(self.history())
        self.write("doc.md", text)
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t)):
            self.rt("run", "doc.md")
        out = self.rt("verify", "doc.md", "a=ok")
        self.assertIn("Not applied", out)
        verifies = [r for r in self.history()[before:] if r["type"] == "verify"]
        self.assertEqual(len(verifies), 1)

    def test_verify_prints_result_line(self):
        self.write("doc.md", "## A\nalpha one\n", spec={"regions": [{"id": "a", "select": "## A"}]},
                   cfg={"min_words": 1, "check_draft": False})
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.08}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t)):
            self.rt("run", "doc.md")
        self.assertIn("a: PASS at stealth_1 · Pangram AI 0.08 · verified", self.rt("verify", "doc.md", "a=ok"))


class Scripts(Project):
    """insights.py and dashboard.py as subprocesses against a small history."""
    def history_file(self, records):
        home = pathlib.Path(self.tmp.name) / "h"
        (home / ".revise-text").mkdir(parents=True)
        (home / ".revise-text" / "history.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return home

    def script(self, name, home, *args):
        import subprocess
        return subprocess.run([sys.executable, str(pathlib.Path(rt.__file__).parent / name), *args],
                              capture_output=True, text=True, env={**os.environ, "HOME": str(home)}, check=True).stdout

    def run_record(self, rid, ai, **kw):
        return {"type": "run", "run_id": rid, "ts": "2026-10-20T10:00:00+00:00", "plugin_version": "0.1.0",
                "humanize_writing_commit": "abc", "mode": "inline", "source": "a.md", "region": "w", "spec": "s",
                "pass_threshold": 0.3, "steps": [{"step": "draft", "pangram_ai": ai}], "result": "REVIEW",
                "pangram_checks": 1, "stealth_cost_usd": 0, "critique": [], **kw}

    def test_insights_since_threshold_and_first_try(self):
        home = self.history_file([
            self.run_record("r1", 0.4),
            {"type": "verify", "run_id": "r1", "ts": "2026-10-20T10:01:00+00:00", "verified": False,
             "mismatches": ["not verified"], "final": False},
            {"type": "verify", "run_id": "r1", "ts": "2026-10-20T10:02:00+00:00", "verified": True,
             "mismatches": [], "final": False}])
        out = json.loads(self.script("insights.py", home, "--since", "2026-10-01"))
        self.assertEqual(out["regions"], 1)
        self.assertEqual(out["draft_pass_rate"], 0.0)
        self.assertEqual(out["verified_first_try"], 1.0)
        self.assertEqual(json.loads(self.script("insights.py", home, "--since", "2026-10-21"))["regions"], 0)

    def test_dashboard_escapes_record_text(self):
        home = self.history_file([self.run_record("r1", 0.4, text_before="Start a comment with `<!--` and load "
                                                  "code with a `<script>` tag.", text_after="x")])
        self.script("dashboard.py", home, "--no-open")
        page = (home / ".revise-text" / "dashboard.html").read_text()
        data = page.split("const RUNS = ", 1)[1].split(";\n", 1)[0]
        self.assertNotIn("<", data)
        self.assertIn("\\u003c!--", data)


class ReviewRound2(Project):
    """Regression tests for the second review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    def guarded(self, text):
        return rt.protect(text, rt.exact_items(text, {}))[0]

    def test_more_things_are_protected(self):
        text = ("-   Clean up first:\n\n    ```bash\n    rm -rf build\n    ```\n\nRun ./scripts/run.sh, then "
                "../shared/config.yaml and logs/app.log; see [the guide](docs/setup.md) and summary.csv.\n")
        g = self.guarded(text)
        for item in ["rm -rf build", "./scripts/run.sh", "../shared/config.yaml", "logs/app.log",
                     "[the guide](docs/setup.md)", "summary.csv"]:
            self.assertNotIn(item, g, item)
        self.assertIn("Clean up first", g)

    def test_items_at_region_edges(self):
        for draft in ("  ```\n  rm -rf build\n  ```\nthen tidy up the rest of the folder by hand when done\n",
                      "the table below keeps every limit as written in the spec\n| a | at most ten |  \n"):
            with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
                 mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("by hand", "manually")
                                                                        .replace("as written", "exactly"))):
                res = rt.humanize(draft, rt.exact_items(draft, {}), self.cfg)
            guarded, _ = rt.protect(draft.strip(), rt.exact_items(draft, {}))
            self.assertNotIn("rm -rf", guarded)
            self.assertNotIn("at most ten", guarded)
            self.assertEqual(res["status"], "PASS")

    def test_literal_items_from_the_rewriter_are_rejected(self):
        draft = "Keep at most 200 rows in the output and nothing more than that please.\n"
        for rewrite in (lambda t: t.replace("nothing more", "write to /tmp/out.csv by 300, nothing more"),
                        lambda t: t.replace("[[E0]] rows", "[[E0]] rows (200 is the cap)")):
            with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
                 mock.patch.object(rt, "stealth", fake_stealth(rewrite)):
                res = rt.humanize(draft, rt.exact_items(draft, {}), {**self.cfg, "length_ratio": [0.5, 2]})
            self.assertEqual(res["status"], "REVIEW")
            self.assertTrue(all(st.get("rejected") == "exact items" for st in res["steps"]))

    def test_final_keeps_regions_already_verified(self):
        text = "## A\nalpha one\n## B\nalpha two\n"
        spec = {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B"}]}
        self.write("doc.md", text, spec=spec, cfg={"min_words": 1, "check_draft": False})
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("alpha", "ALPHA"))):
            self.rt("run", "doc.md")
            self.assertIn("Not applied", self.rt("verify", "doc.md", "a=ok", "b=R1: lost"))
            self.rt("retry", "doc.md", "b")
        out = self.rt("verify", "doc.md", "b=R1: still lost", "--final")
        self.assertIn("a: PASS", out)
        self.assertEqual(pathlib.Path("doc.md").read_text(), "## A\nALPHA one\n## B\nalpha two\n")

    def test_stamps_survive_moving_the_project(self):
        import shutil
        self.write("tasks/001/instruction.md", "x\n", cfg=CFG)
        self.rt("baseline")
        dest = pathlib.Path(self.tmp.name).parent / (pathlib.Path(self.tmp.name).name + "-moved")
        shutil.copytree(".", dest)
        try:
            os.chdir(dest)
            self.assertIn("tasks/001/instruction.md: ok", self.rt("status"))
        finally:
            os.chdir(self.tmp.name)
            shutil.rmtree(dest)

    def test_template_does_not_enforce(self):
        tpl = json.loads((pathlib.Path(rt.SKILL).parent.parent / "templates" / "config.json").read_text())
        self.assertFalse(tpl["enforce"])

    def test_ambiguous_selection_refused(self):
        with self.assertRaises(SystemExit):
            rt.spans("## A\nx\n## B\ny\n", {"regions": [{"id": "r", "select": "## A"}, {"id": "r", "select": "## B"}]})
        with self.assertRaises(SystemExit):
            rt.spans("## Notes\nx\n## Notes\ny\n", {"regions": [{"id": "n", "select": "## Notes"}]})

    def test_frontmatter_and_setext(self):
        text = "---\n# Setup\nk: v\n---\n# Setup\nalpha\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "# Setup"}]})["s"]
        self.assertEqual(text[a:b], "alpha\n")
        text = "## Overview\nalpha\n\nInstallation\n============\nsteps\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "o", "select": "## Overview"}]})["o"]
        self.assertEqual(text[a:b], "alpha\n\n")
        for fm in ("+++\ntitle = 'x'\n+++\n", "---\ntitle: x\n...\n"):
            a, _, *_ = rt.spans(fm + "body\n", {"regions": [{"id": "w", "select": "whole"}]})["w"]
            self.assertEqual(a, len(fm))
        self.assertNotIn("====", self.guarded("Title\n=====\nbody text here\n"))

    def test_unchanged_mixed_region_bytes_kept(self):
        text = b"## S\r\nshort one\r\nshort two\nshort three\r\n## T\nalpha\n"
        out = self.flow(text, {"regions": [{"id": "s", "select": "## S"}]}, rewrite=lambda t: t, raw=True)
        self.assertEqual(out, text)

    def test_ratio_not_rounded(self):
        draft = " ".join(["word"] * 200) + "\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: " ".join(t.split()[:159]))):
            res = rt.humanize(draft, [], self.cfg)
        self.assertEqual(res["steps"][0]["rejected"], "length")

    def test_tag_fallback(self):
        text = "[[E1]] [[X1]] [[Q1]] [[Z1]] and /srv/data"
        guarded, mapping = rt.protect(text, ["/srv/data"])
        self.assertEqual(list(mapping), ["[[EAA0]]"])
        self.assertEqual(rt.restore(guarded, mapping), text)

    def test_run_record_keeps_requirements(self):
        self.flow("## A\nalpha one\n", {"regions": [{"id": "a", "select": "## A"}], "requirements": ["R1: `alpha`"]})
        run = [r for r in self.history() if r["type"] == "run"][-1]
        self.assertEqual(run["requirement_list"], ["R1: `alpha`"])


class ScriptsRound2(Project):
    history_file, script, run_record = Scripts.history_file, Scripts.script, Scripts.run_record
    def test_bad_history_lines_are_skipped(self):
        home = self.history_file([self.run_record("r1", 0.4)])
        with open(home / ".revise-text" / "history.jsonl", "a") as fh:
            fh.write('{"type": "run", "run_id": "half\nnull\n[1]\n')
        self.assertEqual(json.loads(self.script("insights.py", home))["regions"], 1)
        self.script("dashboard.py", home, "--no-open")


class ReviewRound3(Project):
    """Regression tests for the third review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow

    def attempt(self, draft, rewrite, spec=None, **cfg):
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(rewrite)):
            return rt.humanize(draft, rt.exact_items(draft, spec or {}), {**self.cfg, "length_ratio": [0.3, 3], **cfg})

    def mapping(self, draft):
        return rt.protect(draft.strip(), rt.exact_items(draft, {}))[1]

    def test_changed_items_rejected_on_restored_text(self):
        draft = ("Logs are in /data/logs and the report keeps at most 200 rows; exit code 1 means failure.\n\n"
                 "### Configure\nEdit the file.\n\n| name | type |\n|---|---|\n| ts | int |\n\n"
                 "See [the guide](docs/setup.md) and ![diagram](img/d.png). Call `run()` then copy /src/a to /dst/b.\n")
        changes = [("/data/logs", "~/data/logs"), ("most 200", "most ~200"), ("200 rows", "200+ rows"),
                   ("code 1", "code -1"), ("\n\n### Configure\n", " first, then ### Configure: "),
                   ("| ts | int |", "ts is an int"), ("![diagram]", "[diagram]"), ("See [the", "See the"),
                   ("`run()`", "``run()`"), ("/src/a to /dst/b", "/dst/b to /src/a"),
                   ("Logs are", "Logs ⁦are")]
        for old, new in changes:
            items = rt.exact_items(draft, {})
            lost, problems = rt.item_changes(draft.strip(), rt.INVISIBLE.sub("", draft.strip().replace(old, new)), items)
            self.assertTrue(lost or problems or old == "Logs are", (old, new))

    def test_harmless_rewrites_pass(self):
        case = json.loads((pathlib.Path(rt.SKILL).parent.parent / "tests" / "cases" / "example-task.json").read_text())
        draft, spec = case["draft"], {"requirements": case["requirements"]}
        rewrites = [lambda t: t.replace("Your job is to summarize", "You need to summarize"),
                    lambda t: t.replace("everything else is ignored", "skip the rest"),
                    lambda t: t.replace("Group by minute, so", "Results are grouped by minute:"),
                    lambda t: t + " Then count the totals per minute and level once more.",
                    lambda t: t.replace("Don't include", "Leave out")]
        for rw in rewrites:
            res = self.attempt(draft, rw, spec)
            self.assertEqual(res["status"], "PASS", res["steps"])
        # the rewriter writing a protected item out itself, instead of its token, is fine
        draft = "Keep at most 200 rows in the output, newest first, and nothing else.\n"
        res = self.attempt(draft, lambda t: rt.restore(t, self.mapping(draft)).replace("Keep", "Store"))
        self.assertEqual(res["status"], "PASS", res["steps"])
        # a URL at the end of a sentence can move
        draft = "Read the docs at https://example.com/docs. They explain every option in detail for you.\n"
        res = self.attempt(draft, lambda t: "Every option is explained in detail in the docs at "
                           + next(k for k in self.mapping(draft)) + ".")
        self.assertEqual(res["status"], "PASS", res["steps"])

    def test_plain_words_from_requirements_only_backticked(self):
        items = rt.exact_items("Count the rows; the `count` column holds it.", {"requirements": ["R1: `count` and `ERROR`"]})
        self.assertIn("`count`", items)
        self.assertNotIn("count", items)
        self.assertIn("ERROR", rt.spec_items({"requirements": ["R1: `ERROR`"]}))

    def test_selection_edge_cases(self):
        text = "## Setup\nalpha\n<!-- TODO\n## Old setup\nRun as root.\n-->\nThen configure.\n## Next\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertTrue(text[a:b].endswith("Then configure.\n"))
        self.assertIn("<!-- TODO\n## Old setup\nRun as root.\n-->", rt.exact_items(text[a:b], {}))
        text = "## Setup\nalpha\n ## Usage\nRun daily.\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertEqual(text[a:b], "alpha\n")
        self.assertEqual(rt.spans(text, {"regions": [{"id": "u", "select": "## Usage"}]})["u"][:2], (len(text) - 11, len(text)))
        text = "## A\n    ```\n    indented code\n## B\nbeta\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
        self.assertEqual(text[a:b], "    ```\n    indented code\n")
        for before in ("- item", "> quote", "- item\n  continued"):
            text = f"## A\n{before}\n---\nmore\n"
            a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
            self.assertEqual(text[a:b], f"{before}\n---\nmore\n", before)
        for heading in ("Notes for\n  reviewers", "Piping: grep | sort", "  continued",
                        "Release notes for\n2026. Overview", "| a | b |"):   # setext headings (CommonMark)
            text = f"## A\nalpha\n\n{heading}\n---\nmore\n"
            a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
            self.assertEqual(text[a:b], "alpha\n\n", heading)
        text = "## A\nalpha\n\nFirst line\nsecond line\n===\nrest\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
        self.assertEqual(text[a:b], "alpha\n\n")

    def test_unlisted_marker_block_kept(self):
        text = "alpha intro\n<!-- revise:start id=legal -->\nalpha legal text\n<!-- revise:end -->\nalpha outro\n"
        out = self.flow(text, {"regions": [{"id": "w", "select": "whole"}]}).decode()
        self.assertIn("<!-- revise:start id=legal -->\nalpha legal text\n<!-- revise:end -->", out)
        self.assertIn("ALPHA intro", out)

    def test_retry_never_worse_than_draft(self):
        self.write("doc.md", "## A\nalpha one two\n", spec={"regions": [{"id": "a", "select": "## A"}]},
                   cfg={"min_words": 1, "stealth_attempts": 1})
        scores = iter([0.6, 0.7, 0.95])
        with mock.patch.object(rt, "pangram", side_effect=lambda *a: {"pangram_ai": next(scores)}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("one", "uno"))):
            self.rt("run", "doc.md")
            self.rt("retry", "doc.md", "a")
        self.rt("verify", "doc.md", "a=ok")
        self.assertEqual(pathlib.Path("doc.md").read_text(), "## A\nalpha one two\n")

    def test_more_protection(self):
        text = ("<details>\n<summary>More</summary>\nbody text\n</details>\n\n  ## Indented\n\n"
                "name | type\n--- | ---\nts | int\n\n    | x | y |\n")
        g = rt.protect(text, rt.exact_items(text, {}))[0]
        for item in ["<details>", "</summary>", "## Indented", "name | type", "ts | int", "| x | y |"]:
            self.assertNotIn(item, g, item)
        self.assertIn("body text", g)

    def test_crlf_from_rewriter(self):
        draft = "alpha beta gamma\ndelta epsilon zeta\n"
        res = self.attempt(draft, lambda t: t.replace("\n", "\r\n"))
        self.assertNotIn("\r", res["out"])

    def test_status_sees_edits_inside_regions_only(self):
        self.flow("---\nt: x\n---\nalpha body\n", {"regions": [{"id": "w", "select": "whole"}]})
        p = pathlib.Path("doc.md")
        (rt.ROOT / "config.json").write_text(json.dumps({"targets": ["doc.md"]}))
        p.write_text(p.read_text() + "new unrevised paragraph\n")
        self.assertIn("edited after revising: w", self.rt("status"))
        p.write_text("---\nt: y\n---\nALPHA body\n")
        self.assertIn("doc.md: ok", self.rt("status"))
        for f in (rt.ROOT / "stamps").iterdir():      # start over with a new file
            f.unlink()
        self.flow("## A\nalpha one\n## B\n<!-- revise:start id=m -->\nalpha two\n<!-- revise:end -->\nkeep\n",
                  {"regions": [{"id": "a", "select": "## A"}, {"id": "m", "select": "marker"}]})
        (rt.ROOT / "config.json").write_text(json.dumps({"targets": ["doc.md"]}))
        text = p.read_text()
        self.assertEqual(text, "## A\nALPHA one\n## B\nALPHA two\n\nkeep\n")   # still two paragraphs
        p.write_text(text.replace("keep", "kept"))
        self.assertIn("doc.md: ok", self.rt("status"))
        p.write_text(text.replace("ALPHA two", "ALPHA two and more"))
        self.assertIn("edited after revising: m", self.rt("status"))


class ReviewRound4(Project):
    """Regression tests for the fourth review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow

    def check(self, draft, old, new):
        draft = draft.strip()
        lost, problems = rt.item_changes(draft, rt.INVISIBLE.sub("", draft.replace(old, new)), rt.exact_items(draft, {}))
        return bool(lost or problems)

    def test_numbers_with_sign_unit_percent_currency(self):
        d = "Set the offset to -5 hours, keep 50% of rows, cap memory at 512MB, time out after 30s and pay $20 at most."
        for old, new in [("-5", "5"), ("50%", "50"), ("512MB", "256MB"), ("30s", "90s"), ("$20", "20"),
                         ("pay $20", "pay ≈$20"), ("to -5", "to −5"), ("rows", "rowś")]:
            self.assertTrue(self.check(d, old, new) or old == "rows", (old, new))
        self.assertIn("512MB", rt.exact_items(d, {}))

    def test_harmless_number_moves(self):
        d = "Keep at most 200 rows. Exit code 1 means failure."
        for old, new in [("at most 200 rows", "a 200-row maximum"), ("Exit code 1 means failure.", "Failure is exit code 1."),
                         ("200 rows.", "200 rows, newest first."), ("at most 200", "at most (200)")]:
            self.assertFalse(self.check(d, old, new), (old, new))

    def test_indented_blocks_stay_on_their_lines(self):
        d = "1. Install the tools:\n\n    ```bash\n    pip install x\n    ```\n\n2. Then run it.\n\n   ## Release checklist\n\nDone."
        self.assertTrue(self.check(d, "tools:\n\n    ```bash", "tools: ```bash"))
        self.assertTrue(self.check(d, "\n    ```bash", "\n```bash"))
        self.assertTrue(self.check(d, "it.\n\n   ## Release", "it. ## Release"))

    def test_blank_line_after_table_kept(self):
        d = "| key | value |\n|---|---|\n| rows | 200 |\n\nRows past the limit are dropped."
        self.assertTrue(self.check(d, "| 200 |\n\nRows", "| 200 |\nRows"))
        self.assertFalse(self.check(d, "Rows past the limit are dropped.", "Extra rows get dropped."))

    def test_stray_indented_fence(self):
        text = "## Task\nalpha\n\n    ```\n    indented code\n\n## Notes\n```\nkeep alpha code\n```\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "t", "select": "## Task"}]})["t"]
        self.assertEqual(text[a:b], "alpha\n\n    ```\n    indented code\n\n")
        text = "## Task\n    ```\nalpha\n~~~\n# install deps\n~~~\nmore\n# Next\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "t", "select": "## Task"}]})["t"]
        self.assertTrue(text[a:b].endswith("more\n"))

    def test_marker_blocks_never_cut(self):
        text = "## Task\nalpha\n<!-- revise:start id=legal -->\nterms\n## After\nmore\n<!-- revise:end -->\n"
        with self.assertRaises(SystemExit):
            rt.spans(text, {"regions": [{"id": "t", "select": "## Task"}]})
        with self.assertRaises(SystemExit):
            rt.spans("alpha\n<!-- revise:end -->\n", {"regions": [{"id": "w", "select": "whole"}]})

    def test_raw_html_blocks(self):
        text = "## Task\nalpha\n<pre>\n# not a heading\n</pre>\nmore\n## Next\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "t", "select": "## Task"}]})["t"]
        self.assertTrue(text[a:b].endswith("more\n"))
        self.assertNotIn("not a heading", rt.protect(text[a:b], rt.exact_items(text[a:b], {}))[0])


class ReviewRound5(Project):
    """Regression tests for the fifth review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check = ReviewRound4.check

    def guarded(self, text):
        return rt.protect(text, rt.exact_items(text, {}))[0]

    def test_fences_in_quotes_and_list_items(self):
        text = ("## Setup\nalpha\n\n> [!WARNING]\n> ```bash\n> git clean -fdx\n> ```\n\n- ```bash\n  pip install requests==2.31\n"
                "  # install deps\n  ```\n\nalpha after\n\n## Usage\nalpha usage\n")
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertTrue(text[a:b].endswith("alpha after\n\n"))
        g = self.guarded(text[a:b])
        self.assertNotIn("git clean", g)
        self.assertNotIn("pip install", g)
        self.assertIn("alpha after", g)
        self.assertEqual(rt.spans(text, {"regions": [{"id": "u", "select": "## Usage"}]})["u"][:2],
                         (len(text) - len("alpha usage\n"), len(text)))

    def test_inline_code_spans(self):
        g = self.guarded("Run `python -m\nvenv .venv` then ``echo `date` `` and go.")
        self.assertNotIn("venv", g)
        self.assertNotIn("date", g)
        self.assertIn("then", g)

    def test_selection_with_html_and_comments(self):
        text = "## Setup\nUse `<!--` to start a comment.\n\n## Usage\nalpha\n\n## Notes\nx\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertEqual(text[a:b], "Use `<!--` to start a comment.\n\n")
        text = "## Setup\nalpha\n<details>\n# not a heading\n</details>\n\nmore\n## Next\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "s", "select": "## Setup"}]})["s"]
        self.assertTrue(text[a:b].endswith("more\n"))
        text = "## A\nalpha\n\n<!-- note -->\nHeading two\n---\nbody\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
        self.assertEqual(text[a:b], "alpha\n\n<!-- note -->\n")
        text = "---\nk: v\n---\nTitle\n=====\n## A\nalpha\n"
        self.assertNotIn("Title", self.guarded(text[text.index("Title"):]))
        text = "## A\n```\ncode\n    ```\n# still code\n```\nafter\n## B\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
        self.assertTrue(text[a:b].endswith("after\n"))

    def test_status_edge_after_marker_region(self):
        self.flow("intro\n<!-- revise:start id=m -->\nalpha body\n<!-- revise:end -->\noutro\n",
                  {"regions": [{"id": "m", "select": "marker"}]})
        (rt.ROOT / "config.json").write_text(json.dumps({"targets": ["doc.md"]}))
        p = pathlib.Path("doc.md")
        text = p.read_text()
        self.assertEqual(text, "intro\n\nALPHA body\n\noutro\n")
        p.write_text(text.replace("ALPHA body\n", "ALPHA body\nappended line\n"))
        self.assertIn("edited after revising: m", self.rt("status"))
        p.write_text(text.replace("outro", "outro changed"))
        self.assertIn("doc.md: ok", self.rt("status"))

    def test_earlier_regions_stay_tracked(self):
        self.flow("<!-- revise:start id=a -->\nalpha one\n<!-- revise:end -->\nmid\n<!-- revise:start id=b -->\nalpha two\n<!-- revise:end -->\n",
                  {"regions": [{"id": "a", "select": "marker"}, {"id": "b", "select": "marker"}]})
        p = pathlib.Path("doc.md")
        self.assertEqual(p.read_text(), "ALPHA one\n\nmid\n\nALPHA two\n")
        p.write_text("ALPHA one\n\nmid\n\n<!-- revise:start id=b -->\nALPHA two alpha\n<!-- revise:end -->\n")
        self.flow(p.read_text(), {"regions": [{"id": "b", "select": "marker"}]})
        (rt.ROOT / "config.json").write_text(json.dumps({"targets": ["doc.md"]}))
        self.assertIn("doc.md: ok", self.rt("status"))
        p.write_text(p.read_text().replace("ALPHA one", "ALPHA one, edited"))
        self.assertIn("edited after revising: a", self.rt("status"))

    def test_identifiers_with_digits(self):
        d = "Use v1.2 with sha256 on Python3, mask 0x1F and IPv4 only."
        for old, new in [("v1.2", "v1.3"), ("sha256", "sha512"), ("Python3", "Python2"), ("0x1F", "0x2F"), ("IPv4", "IPv6")]:
            self.assertTrue(self.check(d, old, new), old)

    def test_unicode_signs(self):
        d = "Start at 5 and stop at –40 degrees; read /data/logs only."
        for old, new in [("at 5", "at –5"), ("at 5", "at －5"), ("at 5", "at ∼5"), ("at 5", "at ≦5"),
                         ("–40", "40"), ("/data/logs only", "/data/logs* only")]:
            self.assertTrue(self.check(d, old, new), (old, new))
        self.assertFalse(self.check("Keep **200** rows.", "**200**", "200"))

    def test_draft_invisible_chars_kept(self):
        draft = "می‌خواهم and the family \U0001F468‍\U0001F469 waits here for you\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t + "​")):
            res = rt.humanize(draft, rt.exact_items(draft, {}), self.cfg)
        self.assertEqual(res["out"], draft)

    def test_url_alone_on_a_line_can_reflow(self):
        d = "Read the guide at\nhttps://example.com/guide\nbefore you start the job."
        self.assertFalse(self.check(d, "at\nhttps://example.com/guide\nbefore", "at https://example.com/guide before"))
        self.assertTrue(self.check("Intro.\n\n## Setup\n\nBody.", "\n\n## Setup", " ## Setup"))


class ScriptsRound5(Project):
    history_file, script, run_record = Scripts.history_file, Scripts.script, Scripts.run_record

    def test_malformed_records_are_cleaned_or_skipped(self):
        home = self.history_file([self.run_record("r1", 0.4), {**self.run_record("r2", 0.4), "ts": "bad"},
                                  {**self.run_record("r3", 0.4), "steps": None, "best_pangram_ai": "x"}])
        out = json.loads(self.script("insights.py", home))
        self.assertEqual(out["regions"], 2)
        self.script("dashboard.py", home, "--no-open")


class ReviewRound6(Project):
    """Regression tests for the sixth review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check = ReviewRound4.check

    def status(self):
        (rt.ROOT / "config.json").write_text(json.dumps({"targets": ["doc.md"]}))
        return self.rt("status")

    def test_reference_links(self):
        d = "Read [the setup guide][guide] and [the FAQ] first.\n\n[guide]: https://example.com/setup\n[the FAQ]: https://example.com/faq"
        for old, new in [("[guide] and", "[manual] and"), ("[the FAQ] first", "the FAQ first"),
                         ("[guide]: https", "[manual]: https"), ("first.\n\n[guide]", "first. [guide]")]:
            self.assertTrue(self.check(d, old, new), (old, new))
        self.assertFalse(self.check(d, "Read [the", "Start by reading [the"))

    def test_appended_section_revised_clears_neighbour(self):
        self.flow("## A\nalpha one\n", {"regions": [{"id": "a", "select": "## A"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text() + "\n## B\nalpha two\n")
        self.assertIn("doc.md: ok", self.status())          # a new section of the same level isn't an edit of A
        self.flow(p.read_text(), {"regions": [{"id": "b", "select": "## B"}]})
        self.assertIn("doc.md: ok", self.status())
        p.write_text(p.read_text().replace("ALPHA one", "ALPHA one edited"))
        self.assertIn("edited after revising: a", self.status())

    def test_deleted_section_and_baseline(self):
        self.flow("## Old\nalpha old\n\n## Keep\nalpha keep\n", {"regions": [{"id": "old", "select": "## Old"}]})
        p = pathlib.Path("doc.md")
        p.write_text("## Keep\nalpha keep\n")
        self.assertIn("edited after revising: old", self.status())
        out = self.flow(p.read_text(), {"regions": [{"id": "keep", "select": "## Keep"}]})
        self.assertIn("doc.md: ok", self.status())
        p.write_text("## Keep\nALPHA keep\nhand edit\n")
        self.assertIn("edited after revising", self.status())
        self.rt("baseline", "doc.md")
        self.assertIn("doc.md: ok", self.status())

    def test_partial_overlap_keeps_tracking_the_rest(self):
        self.flow("## Task\nalpha first\n\nalpha second\n", {"regions": [{"id": "t", "select": "## Task"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("ALPHA second", "<!-- revise:start id=m -->\nALPHA second alpha\n<!-- revise:end -->"))
        self.flow(p.read_text(), {"regions": [{"id": "m", "select": "marker"}]})
        self.assertIn("doc.md: ok", self.status())
        p.write_text(p.read_text().replace("ALPHA first", "ALPHA first, edited"))
        self.assertIn("edited after revising: t", self.status())

    def test_suffixes_on_names(self):
        d = "Copy config.yaml to summary.csv with v1.2, keep 200 rows and sort by `ts` first."
        for old, new in [("config.yaml to", "config.yaml.example to"), ("summary.csv with", "summary.csv.bak with"),
                         ("v1.2,", "v1.2-beta,"), ("summary.csv with", "summary.csv-old with")]:
            self.assertTrue(self.check(d, old, new), (old, new))
        for old, new in [("keep 200 rows", "keep a 200-row cap"), ("by `ts` first", "`ts`-first")]:
            self.assertFalse(self.check(d, old, new), (old, new))

    def test_dashes_and_emphasis(self):
        self.assertFalse(self.check("Write 150–200 words.", "150–200 words", "between 150 and 200 words"))
        self.assertFalse(self.check("The cap—200 rows—is strict.", "cap—200 rows—is", "cap of 200 rows is"))
        self.assertFalse(self.check("Keep *200* rows.", "*200* rows", "200 rows"))
        self.assertTrue(self.check("Cool to –40 degrees.", "–40", "40"))

    def test_thematic_break_is_not_frontmatter(self):
        text = "---\n\nIntro paragraph.\n\nUsage\n---\n\nBody text.\n"
        self.assertEqual(rt.spans(text, {"regions": [{"id": "w", "select": "whole"}]})["w"][0], 0)
        self.assertEqual(len(rt.structure(text)["headings"]), 1)

    def test_lone_cr(self):
        text = "Intro\rstill\n\n## A\n\nalpha text\n\n## B\nbeta\n"
        a, b, *_ = rt.spans(text, {"regions": [{"id": "a", "select": "## A"}]})["a"]
        self.assertEqual(text[a:b], "\nalpha text\n\n")

    def test_repeated_lines_not_blamed(self):
        old = "Alfa words\n## H\n## H\n## H\n## H\n"
        new = "Alfa words\n## H\nsame\n## H\n## H\n## H\n"
        self.assertFalse(rt.touched(rt.line_diff(old, new), [0, 10], old, new))
        new2 = "Alfa words\n## H\n## H\n## H\n## H\nadded\n"
        self.assertFalse(rt.touched(rt.line_diff(old, new2), [0, 10], old, new2))
        new3 = "Alfa words\nmore\n## H\n## H\n## H\n## H\n"
        self.assertTrue(rt.touched(rt.line_diff(old, new3), [0, 10], old, new3))


class ScriptsRound6(Project):
    history_file, script, run_record = Scripts.history_file, Scripts.script, Scripts.run_record

    def test_odd_step_fields(self):
        home = self.history_file([{**self.run_record("r1", 0.4),
                                   "steps": [{"step": "stealth_1", "rejected": {"x": 1}, "unexpected": [{"y": 2}]}]}])
        self.assertEqual(json.loads(self.script("insights.py", home))["regions"], 1)


class ReviewRound7(Project):
    """Regression tests for the seventh review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check, status = ReviewRound4.check, ReviewRound6.status

    def test_frontmatter_with_blank_line(self):
        text = "---\ntitle: Log summary\n\nauthor: Mike\n---\n\nBody text here.\n"
        self.assertEqual(text[rt.spans(text, {"regions": [{"id": "w", "select": "whole"}]})["w"][0]:], "\nBody text here.\n")
        prose = "---\n\nIntro: a paragraph of prose.\nMore prose here.\n---\n"
        self.assertEqual(rt.spans(prose, {"regions": [{"id": "w", "select": "whole"}]})["w"][0], 0)

    def test_same_id_re_revision_keeps_tracking_the_rest(self):
        self.flow("<!-- revise:start id=body -->\nalpha p1\n\nalpha p2\n\nalpha p3\n<!-- revise:end -->\n",
                  {"regions": [{"id": "body", "select": "marker"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("ALPHA p2", "<!-- revise:start id=body -->\nALPHA p2 alpha\n<!-- revise:end -->"))
        self.flow(p.read_text(), {"regions": [{"id": "body", "select": "marker"}]})
        self.assertIn("doc.md: ok", self.status())
        p.write_text(p.read_text().replace("ALPHA p1", "ALPHA p1 edited"))
        self.assertIn("edited after revising: body", self.status())

    def test_hand_paragraph_before_marker_region_not_absorbed(self):
        self.flow("Intro.\n\n<!-- revise:start id=late -->\nalpha late\n<!-- revise:end -->\n\n## C\nalpha c\n",
                  {"regions": [{"id": "late", "select": "marker"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("ALPHA late", "A note typed by hand.\n\nALPHA late"))
        self.assertIn("doc.md: ok", self.status())
        self.flow(p.read_text(), {"regions": [{"id": "c", "select": "## C"}]})
        p.write_text(p.read_text().replace("typed by hand", "typed by hand, edited"))
        self.assertIn("doc.md: ok", self.status())

    def test_edit_marks_only_its_piece_and_clears(self):
        self.flow("## A\nalpha a\n\n## B\nalpha b\n\n## C\nalpha c\n", {"regions": [{"id": "whole", "select": "whole"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("ALPHA b", "ALPHA b, hand edit"))
        self.flow(p.read_text().replace("ALPHA c", "alpha c"), {"regions": [{"id": "c", "select": "## C"}]})
        self.assertIn("edited after revising: whole", self.status())
        self.flow(p.read_text().replace("ALPHA b, hand edit", "alpha b, hand edit"), {"regions": [{"id": "b", "select": "## B"}]})
        self.assertIn("doc.md: ok", self.status())

    def test_blank_line_before_next_section_ignored(self):
        self.flow("## A\nalpha a\n## B\nalpha b1\n\nalpha b2\n\nalpha b3\n",
                  {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("\n## B", "\n\n## B"))
        self.assertIn("doc.md: ok", self.status())

    def test_and_or_is_not_a_path(self):
        d = "Read the logs in logs/app.log and write the summary."
        self.assertFalse(self.check(d, "and write", "and/or write"))
        self.assertIn("logs/app.log", rt.exact_items(d, {}))

    def test_deleted_region_note_when_adjacent(self):
        self.flow("## A\nalpha a\n\n## B\nalpha b\n\n## C\nalpha c\n",
                  {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("## B\nALPHA b\n\n", ""))
        rt.spec_for("doc.md").write_text(json.dumps({"regions": [{"id": "c", "select": "## C"}]}))
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: t.replace("alpha", "ALPHA"))):
            self.rt("run", "doc.md")
        self.assertIn("revised region b is no longer in the file", self.rt("verify", "doc.md", "c=ok"))
        self.assertIn("doc.md: ok", self.status())


class FakeMcp:
    """A local stand-in for StealthGPT's MCP server, with switches for the failures a real one can have."""
    def __init__(self, preservation=True, billing="subscription", expire_once=False, expire_after=None,
                 drop_once=False, list_404=False, status=None, noisy=False, string_error=False, reorder=False,
                 drop_start=False):
        import http.server, threading
        self.calls, self.sessions, self.lock, self.texts = [], 0, threading.Lock(), {}
        self.__dict__.update(preservation=preservation, billing=billing, expire_once=expire_once,
                             expire_after=expire_after, drop_once=drop_once, list_404=list_404, status=status,
                             noisy=noisy, string_error=string_error, reorder=reorder, drop_start=drop_start,
                             expired=set(), polls={})
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, obj, sse=False, session=None):
                if sse or fake.noisy:
                    pre = ('event: ping\ndata: {"jsonrpc": "2.0", "id": %d, "method": "ping"}\n\n: keep-alive\n\n'
                           'data: not json\n\n' % obj["id"]) if fake.noisy and "id" in obj else ""
                    body, kind = (pre + f"event: message\ndata: {json.dumps(obj)}\n\n").encode(), "text/event-stream"
                else:
                    body, kind = json.dumps(obj).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def fail(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["Authorization"] == "Bearer s"
                session = self.headers.get("Mcp-Session-Id")
                method, params = msg.get("method"), msg.get("params") or {}
                with fake.lock:
                    fake.calls.append((method, params, session))
                    if method == "initialize":
                        fake.sessions += 1
                        new = f"s{fake.sessions}"
                    tool_calls = sum(1 for c in fake.calls if c[0] == "tools/call")
                    if fake.expire_after and tool_calls > fake.expire_after and session == "s1":
                        fake.expired.add("s1")
                if method == "initialize":
                    return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}},
                                      sse=True, session=new)
                if "id" not in msg:
                    return self.fail(202)
                if session in fake.expired:
                    return self.fail(404)
                if method == "tools/list" and fake.list_404:
                    return self.fail(404)
                if fake.expire_once and method == "tools/call":
                    fake.expire_once = False
                    return self.fail(404)
                if fake.string_error and method == "tools/call":
                    return self.reply({"jsonrpc": "2.0", "id": msg["id"], "error": "quota exceeded"})
                if method == "tools/list":
                    props = {"text": {}, "model": {}, "outputFormat": {}, "qualityMode": {}}
                    if fake.preservation:
                        props.update(preservation={}, preservationMode={})
                    return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                        {"name": "humanize_text", "inputSchema": {"properties": props}},
                        {"name": "get_run_status", "inputSchema": {"properties": {"runId": {}}}}]}})
                args = params["arguments"]
                if params["name"] == "humanize_text" and fake.drop_start:
                    self.close_connection = True     # the run started, but the reply never came
                    return
                if params["name"] == "humanize_text":
                    with fake.lock:
                        run = f"r{len(fake.texts) + 1}"
                        fake.texts[run] = args["text"]
                    content = [{"type": "text", "text": json.dumps({"runId": run, "status": "queued"})}]
                    return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": content}})
                run = args["runId"]
                with fake.lock:
                    fake.polls[run] = fake.polls.get(run, 0) + 1
                    n, drop = fake.polls[run], fake.drop_once
                    fake.drop_once = False
                if drop:
                    self.close_connection = True     # hang up without an answer
                    return
                text = fake.texts[run].replace("alpha", "ALPHA")
                if fake.reorder:
                    text = " ".join(reversed(text.split(" ")))
                data = {"status": "running"} if n < 2 else (
                    {"status": fake.status} if fake.status else
                    {"status": "completed", "result": text, "wordsSpent": 42,
                     **({"billingMode": fake.billing} if fake.billing else {})})
                self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": {"structuredContent": data}})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/mcp/mcp"

    def close(self):
        self.server.shutdown()


class McpRoute(Project):
    def use(self, fake):
        for patch in (mock.patch.object(rt, "STEALTH_MCP", fake.url), mock.patch.object(rt, "MCP_POLL_SECONDS", 0),
                      mock.patch.dict(rt.MCP_CLIENTS, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(fake.close)
        return fake

    def test_humanize_through_mcp(self):
        fake = self.use(FakeMcp())
        out, spent, billing, _ = rt.stealth("alpha [[E0]] beta", {"preservationMode": "split", "via": "mcp"}, "super")
        self.assertEqual((out, spent, billing), ("ALPHA [[E0]] beta", 42, "subscription"))
        self.assertEqual(fake.sessions, 1)
        args = next(c[1]["arguments"] for c in fake.calls if c[0] == "tools/call")
        self.assertEqual(args, {"text": "alpha [[E0]] beta", "model": "super", "outputFormat": "text",
                                "qualityMode": "quality", "preservation": True, "preservationMode": "split"})
        self.assertTrue(all(c[2] == "s1" for c in fake.calls if c[0] != "initialize"))

    def test_no_preservation_offered(self):
        fake = self.use(FakeMcp(preservation=False))
        rt.stealth("alpha", {"preservationMode": "split", "via": "mcp"}, "super")
        args = next(c[1]["arguments"] for c in fake.calls if c[0] == "tools/call")
        self.assertNotIn("preservation", args)
        self.assertNotIn("preservationMode", args)

    def test_expired_session_restarts(self):
        fake = self.use(FakeMcp(expire_once=True))
        self.assertEqual(rt.stealth("alpha", {"via": "mcp"}, "super")[0], "ALPHA")
        self.assertEqual(fake.sessions, 2)

    def test_billing_in_history(self):
        for billing, usd, plan in (("subscription", 0, 42), ("payg", 0.021, 0)):
            self.use(FakeMcp(billing=billing))
            draft = "alpha one two three four five six seven\n"
            self.write("doc.md", "## A\n" + draft, spec={"regions": [{"id": "a", "select": "## A"}]},
                       cfg={"min_words": 1, "check_draft": False})
            with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}):
                self.rt("run", "doc.md")
            run = [r for r in self.history() if r["type"] == "run"][-1]
            self.assertEqual((run["stealth_cost_usd"], run["stealth_subscription_words"]), (usd, plan), billing)
            self.assertEqual(run["steps"][0]["billing"], billing)

    def test_check_command(self):
        self.use(FakeMcp())
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.9}):
            out = self.rt("check")
        self.assertIn("token preservation: supported", out)
        self.assertIn("billing mode 'subscription'", out)
        self.assertIn("protected items intact", out)
        self.assertIn("Pangram on the rewrite: AI score 0.90 (would not pass", out)


class McpFailures(McpRoute):
    """Round-eight findings: the MCP client under failure, and billing in the history."""
    def parallel(self, n=4):
        with cf.ThreadPoolExecutor(n) as pool:
            return list(pool.map(lambda i: rt.stealth(f"alpha {i}", {"via": "mcp"}, "super"), range(n)))

    def test_session_expiry_with_parallel_regions(self):
        fake = self.use(FakeMcp(expire_after=2))
        self.assertEqual(sorted(r[0] for r in self.parallel()), [f"ALPHA {i}" for i in range(4)])
        self.assertEqual(fake.sessions, 2)

    def test_404_in_handshake_fails_without_hanging(self):
        self.use(FakeMcp(list_404=True))
        with cf.ThreadPoolExecutor(1) as pool:
            fut = pool.submit(rt.stealth, "alpha", {"via": "mcp"}, "super")
            with self.assertRaises(RuntimeError):
                fut.result(timeout=30)

    def test_dropped_connection_on_poll_is_retried(self):
        self.use(FakeMcp(drop_once=True))
        with mock.patch.object(rt.time, "sleep"):
            self.assertEqual(rt.stealth("alpha", {"via": "mcp"}, "super")[0], "ALPHA")

    def test_noisy_streams_and_string_errors(self):
        self.use(FakeMcp(noisy=True))
        self.assertEqual(rt.stealth("alpha", {"via": "mcp"}, "super")[0], "ALPHA")
        rt.MCP_CLIENTS.clear()
        self.use(FakeMcp(string_error=True))
        with self.assertRaisesRegex(RuntimeError, "quota exceeded"):
            rt.stealth("alpha", {"via": "mcp"}, "super")

    def run_region(self):
        self.write("doc.md", "## A\nalpha one two three four five six seven\n",
                   spec={"regions": [{"id": "a", "select": "## A"}]}, cfg={"min_words": 1, "check_draft": False})
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}):
            self.rt("run", "doc.md")
        return [r for r in self.history() if r["type"] == "run"][-1]

    def test_started_run_that_fails_is_logged(self):
        self.use(FakeMcp(status="failed"))
        run = self.run_region()
        self.assertEqual(run["result"], "ERROR")
        self.assertEqual((run["steps"][0]["run_id"], run["steps"][0]["billing"]), ("r1", "unknown"))

    def test_unknown_billing_has_its_own_bucket(self):
        self.use(FakeMcp(billing=None))
        run = self.run_region()
        self.assertEqual((run["stealth_cost_usd"], run["stealth_subscription_words"],
                          run["stealth_unknown_billing_words"]), (0, 0, 42))

    def test_mcp_allows_reordered_items(self):
        self.use(FakeMcp(reorder=True))
        draft = "Use /data/in then /data/out for 200 rows here\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}):
            res = rt.humanize(draft, rt.exact_items(draft, {}), {**rt.DEFAULTS, "min_words": 1, "check_draft": False})
        self.assertEqual(res["status"], "PASS", res["steps"])
        lost, problems = rt.item_changes("copy /src/a to /dst/b", "copy /dst/b to /src/a", ["/src/a", "/dst/b"])
        self.assertEqual(problems, ["protected items reordered"])     # the REST route still requires order


class StatusRound8(Project):
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    status = ReviewRound6.status

    def test_baseline_skips_frontmatter(self):
        self.write("doc.md", "---\ndate: 2026-10-01\n---\nBody text.\n")
        self.rt("baseline", "doc.md")
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("2026-10-01", "2026-10-02"))
        self.assertIn("doc.md: ok", self.status())
        p.write_text(p.read_text().replace("Body", "Body edited"))
        self.assertIn("edited after revising: whole", self.status())

    def test_append_to_file_without_final_newline(self):
        self.flow("## A\nalpha a\n## B\nalpha b two", {"regions": [{"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text() + "\n\n## C\nNew section.")
        self.assertIn("doc.md: ok", self.status())
        p.write_text(p.read_text().replace("ALPHA b two", "ALPHA b two, edited"))
        self.assertIn("edited after revising: b", self.status())

    def test_hand_paragraph_re_revised_clears(self):
        self.flow("## A\nalpha a\n\n## B\nalpha b\n", {"regions": [{"id": "whole", "select": "whole"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("ALPHA a\n", "ALPHA a\n\nTyped by hand alpha.\n"))
        self.flow(p.read_text().replace("ALPHA b", "alpha b"), {"regions": [{"id": "b", "select": "## B"}]})
        self.assertIn("edited after revising: whole", self.status())
        text = p.read_text().replace("Typed by hand alpha.", "<!-- revise:start id=h -->\nTyped by hand alpha.\n<!-- revise:end -->")
        self.flow(text, {"regions": [{"id": "h", "select": "marker"}]})
        self.assertIn("doc.md: ok", self.status())

    def test_deletion_beside_re_revised_block(self):
        self.flow("## A\nalpha one\n\nalpha two\n\nalpha three\n", {"regions": [{"id": "a", "select": "## A"}]})
        p = pathlib.Path("doc.md")
        text = p.read_text().replace("ALPHA two\n\n", "").replace("ALPHA three", "<!-- revise:start id=t -->\nALPHA three\n<!-- revise:end -->")
        self.flow(text, {"regions": [{"id": "t", "select": "marker"}]})
        self.assertIn("edited after revising: a", self.status())


class ScriptsRound8(Project):
    history_file, script, run_record = Scripts.history_file, Scripts.script, Scripts.run_record

    def test_billing_modes_and_null_draft_score(self):
        home = self.history_file([
            {**self.run_record("r1", 0.4), "steps": [{"step": "draft", "pangram_ai": None},
                                                     {"step": "stealth_1", "words_billed": 40, "billing": "subscription", "via": "mcp"}]}])
        out = json.loads(self.script("insights.py", home))
        self.assertEqual(out["billing_modes"], {"subscription": 1})
        self.assertIsNone(out["draft_pass_rate"])


class ReviewRound9(Project):
    """Regression tests for the ninth review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check = ReviewRound4.check

    def test_indented_code_at_region_start(self):
        draft = "\n    make build\n    make install\n\nThat builds the tool and puts it on your path for later use.\n"
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}), \
             mock.patch.object(rt, "stealth", fake_stealth(lambda t: "First run " + t.replace("\n\nThat", " which"))):
            res = rt.humanize(draft, rt.exact_items(draft, {}), {**rt.DEFAULTS, "min_words": 1, "check_draft": False,
                                                                 "length_ratio": [0.3, 3]})
        self.assertEqual(res["status"], "REVIEW", res["steps"])

    def test_other_currencies(self):
        for d, old, new in [("Pay ₹500 now.", "₹500", "500"), ("Pay ₩5000 now.", "₩5000", "5000"),
                            ("Pay 500€ now.", "500€", "500"), ("Pay 500 now.", "500 ", "500₽ ")]:
            self.assertTrue(self.check(d, old, new), (old, new))

    def test_marker_removal_keeps_rendering(self):
        out = self.flow("Intro sentence.\n<!-- revise:start id=b -->\nalpha text here\n<!-- revise:end -->\n---\n",
                        {"regions": [{"id": "b", "select": "marker"}]}).decode()
        self.assertEqual(out, "Intro sentence.\n\nALPHA text here\n\n---\n")
        self.assertEqual(rt.structure(out)["headings"], [])

    def test_blocks_keep_order_on_mcp(self):
        d = "## Input\n\nRead from /data/in.\n\n## Output\n\nWrite to /data/out."
        swapped = "## Input\n\nWrite to /data/out.\n\n## Output\n\nRead from /data/in."
        lost, problems = rt.item_changes(d, swapped, rt.exact_items(d, {}), ordered=False)
        self.assertTrue(problems)
        lost, problems = rt.item_changes(d, "Read from /data/in.\n\n## Input\n\n## Output\n\nWrite to /data/out.",
                                         rt.exact_items(d, {}), ordered=False)
        self.assertTrue(problems)
        d2 = "Copy /src/a, then /src/b, to the target."
        lost, problems = rt.item_changes(d2, "Copy /src/b, then /src/a, to the target.", rt.exact_items(d2, {}), ordered=False)
        self.assertEqual(problems, [])

    def test_frontmatter_keys_with_colons(self):
        text = "---\ntitle: X\nog:image: /img.png\n---\nBody\n"
        self.assertEqual(text[rt.spans(text, {"regions": [{"id": "w", "select": "whole"}]})["w"][0]:], "Body\n")

    def test_relative_paths_and_prose_pairs(self):
        d = "Move data/raw to config/prod, and/or read/write as needed."
        items = rt.exact_items(d, {})
        self.assertIn("data/raw", items)
        self.assertIn("config/prod", items)
        self.assertNotIn("and/or", items)
        self.assertTrue(self.check(d, "data/raw", "data/processed"))

    def test_billing_modes(self):
        self.assertEqual(rt.billed_to({"billing": "metered", "via": "mcp"}), "unknown")
        self.assertEqual(rt.billed_to({"billing": "subscription", "via": "mcp"}), "plan")
        self.assertEqual(rt.billed_to({"billing": "payg", "via": "mcp"}), "credits")


class McpRound9(McpRoute):
    def test_run_lost_after_sending_is_logged(self):
        self.use(FakeMcp(drop_start=True))
        self.write("doc.md", "## A\nalpha one two three four five six seven\n",
                   spec={"regions": [{"id": "a", "select": "## A"}]}, cfg={"min_words": 1, "check_draft": False})
        with mock.patch.object(rt, "pangram", return_value={"pangram_ai": 0.1}):
            self.rt("run", "doc.md")
        run = [r for r in self.history() if r["type"] == "run"][-1]
        self.assertEqual(run["result"], "ERROR")
        self.assertEqual(run["stealth_unknown_billing_words"], 16)

    def test_metered_credits_cost_money(self):
        res = {"steps": [{"step": "stealth_1", "via": "mcp", "billing": "subscription", "words_billed": 40,
                          "metered_words": 10}], "run_id": "x", "draft": "a", "out": "a", "items": [], "status": "PASS"}
        self.write("doc.md", "## A\na\n", spec={"regions": [{"id": "a", "select": "## A"}]})
        with contextlib.redirect_stdout(io.StringIO()):
            rt.save(pathlib.Path("doc.md"), "a", rt.load_spec("doc.md"), res, rt.config(), mode="inline")
        run = self.history()[-1]
        self.assertEqual((run["stealth_cost_usd"], run["stealth_subscription_words"]), (0.005, 40))

    def test_missing_items_warning_looks_at_whole_file_with_several_regions(self):
        spec = {"regions": [{"id": "a", "select": "## A"}, {"id": "b", "select": "## B"}],
                "requirements": ["R1: logs in `/data/logs`", "R2: output in `/out/x.csv`", "R3: uses `/etc/gone`"]}
        self.write("doc.md", "## A\nlogs in /data/logs\n\n## B\noutput in /out/x.csv\n", spec=spec)
        res = {"steps": [], "run_id": "x", "draft": "logs in /data/logs\n", "out": "logs in /data/logs\n",
               "items": [], "status": "PASS"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rt.save(pathlib.Path("doc.md"), "a", rt.load_spec("doc.md"), res, rt.config(), mode="inline")
        self.assertIn("missing these spec items: ['/etc/gone']", out.getvalue())


class PangramErrors(Project):
    def test_no_credits_explained(self):
        def refuse(url, headers, body=None, **kw):
            raise RuntimeError(f"{url} returned 402: {{\"detail\":\"Insufficient credits\"}}")
        with mock.patch.object(rt, "request", refuse), self.assertRaisesRegex(RuntimeError, "separate from Pangram's web plans"):
            rt.pangram("text", "pangram-4.1")


class ReviewRound10(Project):
    """Regression tests for the tenth review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check, status = ReviewRound4.check, ReviewRound6.status

    BLOCKS = ["A plain paragraph of text.", "- first item\n- second item", "1. step one\n2. step two",
              "    indented code line", "> a quoted line", "| a | b |\n|---|---|\n| 1 | 2 |", "## A heading",
              "```\nfenced code\n```", "Another line of prose."]

    def test_rendering_outside_marker_regions_unchanged(self):
        import random
        rng = random.Random(10)
        for n in range(150):
            blocks = [rng.choice(self.BLOCKS) for _ in range(rng.randint(2, 5))]
            i = rng.randrange(len(blocks))
            j = rng.randint(i + 1, min(len(blocks), i + 2))
            gap = lambda: "\n" + "\n" * rng.randint(0, 1)
            parts = blocks[:i] + ["<!-- revise:start id=r -->\n" + gap().join(blocks[i:j]) + "\n<!-- revise:end -->"] + blocks[j:]
            text = "".join(p + gap() for p in parts)
            rt.MD.render(text)
            out = self.flow(text, {"regions": [{"id": "r", "select": "marker"}]}, rewrite=lambda t: t).decode()
            self.assertEqual(rt.rendered(out), rt.rendered(text), (n, text, out))
            self.assertNotIn("revise:", out)

    def test_lists_around_marker_block(self):
        text = ("Install steps:\n\n- Install the tool.\n- Run the setup script.\n\n<!-- revise:start id=r -->\n"
                "- Keep the cache warm between runs.\n- Rotate the logs every day.\n<!-- revise:end -->\n")
        out = self.flow(text, {"regions": [{"id": "r", "select": "marker"}]}, rewrite=lambda t: t).decode()
        self.assertEqual(rt.rendered(out), rt.rendered(text))
        self.assertIn("<!-- -->", out)

    def test_empty_marker_block_between_paragraphs(self):
        text = "First paragraph line.\n<!-- revise:start id=r -->\n<!-- revise:end -->\nSecond paragraph line.\n"
        rt.spec_for("doc.md").parent.mkdir(parents=True, exist_ok=True)
        out = self.flow(text, {"regions": [{"id": "r", "select": "marker"}]}, rewrite=lambda t: t).decode()
        self.assertEqual(rt.rendered(out), rt.rendered(text))

    def test_edge_edit_survives_other_verify(self):
        self.flow("## A\nalpha a\n\n## B\nalpha b\n", {"regions": [{"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text() + "\nAdded by hand.\n")
        self.assertIn("edited after revising: b", self.status())
        self.flow(p.read_text(), {"regions": [{"id": "a", "select": "## A"}]})
        self.assertIn("edited after revising: b", self.status())

    def test_word_pairs_from_the_rewriter(self):
        d = "Split the data into train and test sets before you start."
        self.assertFalse(self.check(d, "train and test", "train/test"))

    def test_leading_dot_on_a_number(self):
        self.assertTrue(self.check("Set the threshold to 5 for now.", "to 5", "to .5"))

    def test_demoted_next_heading(self):
        self.flow("## B\nalpha b\n\n## C\nalpha c\n", {"regions": [{"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("## C", "### C"))
        self.assertIn("edited after revising: b", self.status())


class ReviewRound11(Project):
    """Regression tests for the eleventh review."""
    cfg, flow = ReviewFindings.cfg, ReviewFindings.flow
    check, status = ReviewRound4.check, ReviewRound6.status

    def test_back_to_back_marker_blocks(self):
        text = ("# Guide\n<!-- revise:start id=a -->\nFirst paragraph about the data file.\n<!-- revise:end -->\n"
                "<!-- revise:start id=b -->\nSecond paragraph about the report.\n<!-- revise:end -->\nTail paragraph.\n")
        spec = {"regions": [{"id": "a", "select": "marker"}, {"id": "b", "select": "marker"}]}
        for rewrite in (lambda t: t, lambda t: t.replace("about", "on")):
            out = self.flow(text, spec, rewrite=rewrite).decode()
            self.assertEqual(rt.rendered(out), rt.rendered(rewrite(text)), out)

    def test_directories_with_trailing_slash(self):
        d = "Put the binaries in build/ and the sources in src/lib/, then commit."
        self.assertIn("build/", rt.exact_items(d, {}))
        self.assertIn("src/lib/", rt.exact_items(d, {}))
        for old, new in [("build/", "dist/"), ("build/", "build"), ("src/lib/", "src/")]:
            self.assertTrue(self.check(d, old, new), (old, new))
        self.assertNotIn("w/", rt.exact_items("Ship w/ care.", {}))

    def test_harmless_formatting_around_items(self):
        self.assertFalse(self.check("Keep the newest 200 rows.", "200", "_200_"))
        self.assertFalse(self.check("Count `ERROR` and `WARN` lines.", "`ERROR` and `WARN`", "`ERROR`/`WARN`"))
        self.assertFalse(self.check("## Setup\n\nInstall the tool.", "## Setup\n\nInstall", "## Setup\nInstall"))
        self.assertFalse(self.check("Run it:\n\n```\nmake\n```", "Run it:\n\n```", "Run it:\n```"))
        self.assertTrue(self.check("Intro.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nAfter.", "| 1 | 2 |\n\nAfter", "| 1 | 2 |\nAfter"))

    def test_deleting_the_rest_of_the_file(self):
        for keep_blank in (True, False):
            self.flow("## Setup\nalpha setup\n\n## Usage\nalpha usage\n\n## FAQ\nalpha faq\n",
                      {"regions": [{"id": "s", "select": "## Setup"}]})
            p = pathlib.Path("doc.md")
            text = p.read_text()
            p.write_text(text[:text.index("## Usage")] if keep_blank else text[:text.index("## Usage")].rstrip() + "\n")
            self.assertIn("doc.md: ok", self.status(), keep_blank)

    def test_setext_heading_as_next_section(self):
        self.flow("## B\nalpha b\n\n## C\nalpha c\n", {"regions": [{"id": "b", "select": "## B"}]})
        p = pathlib.Path("doc.md")
        p.write_text(p.read_text().replace("## C", "C\n-"))
        self.assertIn("doc.md: ok", self.status())

    def test_frontmatter_added_on_top(self):
        self.flow("alpha body text\n", {"regions": [{"id": "w", "select": "whole"}]})
        p = pathlib.Path("doc.md")
        p.write_text("---\ntitle: New\n---\n" + p.read_text())
        self.assertIn("doc.md: ok", self.status())

    def test_first_line_deleted_then_rest_re_revised(self):
        self.flow("## Intro\nalpha intro\n\nline one alpha\nline two alpha\nline three alpha\n",
                  {"regions": [{"id": "whole", "select": "whole"}]})
        p = pathlib.Path("doc.md")
        text = p.read_text().replace("line one ALPHA\n", "")
        text = text.replace("line two ALPHA\nline three ALPHA", "<!-- revise:start id=r -->\nline two ALPHA\nline three ALPHA\n<!-- revise:end -->")
        self.flow(text, {"regions": [{"id": "r", "select": "marker"}]})
        self.assertIn("doc.md: ok", self.status())

    def test_unreadable_files_and_broken_state(self):
        self.write("tasks/001/instruction.md", "x\n", cfg=CFG)
        pathlib.Path("tasks/001/instruction.md").write_bytes("café\n".encode("latin-1"))
        self.assertIn("not UTF-8", self.rt("status"))
        self.assertIn("skipped", self.rt("baseline"))
        (rt.ROOT / "config.json").write_text("{bad json")
        self.assertIn("couldn't run", self.rt("guard", stdin="{}"))
        self.assertEqual(self.rt("guard", stdin='{"stop_hook_active": true}'), "")
        (rt.ROOT / "config.json").write_text(json.dumps(CFG))
        pathlib.Path("tasks/001/instruction.md").write_text("ok now\n")
        (rt.ROOT / "stamps").mkdir(exist_ok=True)
        (rt.ROOT / "stamps" / f"{rt.fkey('tasks/001/instruction.md')}.json").write_text("")
        self.assertIn("not revised", self.rt("status"))

    def test_word_limit_checked_before_pangram(self):
        called = mock.Mock(return_value={"pangram_ai": 0.9})
        with mock.patch.object(rt, "pangram", called):
            res = rt.safe_humanize("word " * 3001, [], {**rt.DEFAULTS})
        self.assertEqual(res["status"], "ERROR")
        called.assert_not_called()

    def test_pangram_task_not_retried(self):
        calls = []
        def lost(url, headers, body=None, **kw):
            calls.append(kw.get("safe", True))
            raise rt.SentError("connection failed")
        with mock.patch.object(rt, "request", lost), self.assertRaises(rt.SentError):
            rt.pangram("text", "pangram-4.1")
        self.assertEqual(calls, [False])


class ReviewRound12(Project):
    """Regression tests for the twelfth review."""
    check = ReviewRound4.check

    def unordered(self, d, old, new):
        d = d.strip()
        lost, problems = rt.item_changes(d, d.replace(old, new), rt.exact_items(d, {}), ordered=False)
        return bool(lost or problems)

    def test_footnote_after_reworded_word(self):
        d = "Runs now take ten minutes instead of over an hour[^2].\n\n[^2]: Measured on the build server."
        self.assertFalse(self.unordered(d, "over an hour[^2].", "more than an hour of runtime[^2]."))
        self.assertFalse(self.unordered("See the guide[setup] first.", "guide[setup]", "manual[setup]"))
        self.assertFalse(self.unordered("Response times dropped by 35% overall[^1].", "35% overall[^1]", "35%[^1]"))
        self.assertTrue(self.unordered("See [the diagram](d.png) first.", "See [the", "See ![the"))
        self.assertTrue(self.unordered("Use v1.2 here.", "v1.2", "v1.2b"))
        self.assertTrue(self.unordered("Keep 200 rows.", "200 rows", "200k rows"))

    def test_emphasis_ending_at_an_item(self):
        for d, old, new in [("Keep *at most 200* rows.", "*at most 200* rows", "no more than 200 rows"),
                            ("Keep _at most 200_ rows.", "_at most 200_ rows", "no more than 200 rows"),
                            ("Use *200 rows at most* please.", "*200 rows at most*", "200 rows at most")]:
            self.assertFalse(self.unordered(d, old, new), d)
        self.assertTrue(self.unordered("Read /data/logs only.", "/data/logs only", "/data/logs* only"))

    def test_dates_are_one_item(self):
        d = "The window closes on 2026-11-05 at noon."
        self.assertIn("2026-11-05", rt.exact_items(d, {}))
        self.assertTrue(self.unordered(d, "2026-11-05", "2026-05-11"))
        self.assertFalse(self.unordered(d, "closes on 2026-11-05 at noon", "shuts at noon on 2026-11-05"))

if __name__ == "__main__":
    unittest.main()
