# revise-text

A Claude Code plugin for revising the parts of Markdown files that Claude generates so they read as naturally
written. The Claude that writes the content also revises it and checks that the meaning survived:

1. Claude writes the target regions following the bundled writing rules and saves a **spec**: what each region
   must say.
2. `rt run` checks each region with **Pangram**, falls back to **StealthGPT** (with exact strings protected) when
   needed, and prints each region's requirements beside its revised text.
3. Claude compares them and runs `rt verify`. Only text whose meaning was checked goes back into the file.

## Install

```
/plugin marketplace add mkbuycn/revise-text
/plugin install revise-text@revise-text
```

Choose the **user** scope so every project has it. Or, with the Skills CLI: `npx skills add mkbuycn/revise-text`
(this installs the skill only, without the dashboard, insights commands or finish check).

Set `PANGRAM_KEY` and `STEALTH_KEY` (the token from StealthGPT's API dashboard) in your environment. Python 3.10
or newer is needed. There's nothing
to install: the Markdown parser it uses (markdown-it-py, with mdurl) is bundled in `scripts/_vendor/`.

**StealthGPT billing.** By default `rt` goes through StealthGPT's MCP server, so runs can come out of your web
subscription's words: in StealthGPT's usage settings, set **MCP billing** to your subscription (MCP runs use API
credits until you do). REST API calls always bill API credits; set `"stealth_via": "api"` to use them instead.
**Pangram billing.** Pangram's API needs its own credits (the Developers plan, $0.05 per 100 words, at
https://www.pangram.com/solutions/api); credits from Pangram's web plans can't be used through the API. A
~200-word region usually takes two checks (draft and rewrite); `"check_draft": false` saves the first.

Run `rt check` once after setup: it makes two small Pangram checks (the sample and its rewrite) and one
StealthGPT rewrite, and reports which
balance was billed and whether protected items came through.

## Set up a project

1. Copy [templates/config.json](templates/config.json) to `.revise-text/config.json`. `targets` decides which
   files are covered.
2. Paste [templates/CLAUDE-snippet.md](templates/CLAUDE-snippet.md) into the project's `CLAUDE.md` (or
   `~/.claude/CLAUDE.md` for every project).
3. Add `.revise-text/work/`, `.revise-text/backups/` and `.revise-text/paste/` to `.gitignore`. Keep `specs/`,
   `stamps/` and `config.json` in git.
4. Optional: set `"enforce": true` to turn on the finish check. When Claude tries to finish while a target file is
   unrevised, or was edited after revising, it gets one reminder. In a project that already has target files,
   run `rt baseline` once first so older files don't trigger it. If the check itself can't run (a broken
   `config.json`, say), it tells Claude so once instead of failing, and a target file that isn't UTF-8 is
   reported as unreadable.

`rt` is `python3 <plugin>/skills/revise-text/scripts/rt.py`, run from the project folder.

| Command | What it does |
|---|---|
| `rt run <file> [--critique="a; b"] [--standalone]` | Pangram, then StealthGPT if needed, for every region in the file's spec, in parallel |
| `rt verify <file> <region>=ok …` | All ok: writes the revised regions back, removes markers, stamps the file, and prints one line per region (`whole: PASS at stealth_1 · Pangram AI 0.08 · verified`). `<region>="R3: …"` records a mismatch; add `--final` to keep Claude's draft for mismatched or unchecked regions. An `ok` is remembered until that region is run or retried again |
| `rt retry <file> <region>` | One more StealthGPT attempt for a region (limited by `retries`) |
| `rt backup <file>` | Copies the file into `.revise-text/backups/` |
| `rt status` | Every target file: ok, not revised, or edited after revising (a change inside a revised region, or text added at its edge; blank lines and edits elsewhere in the file don't count). Regions revised by earlier verifies stay tracked, minus any part revised again; a revised region that was deleted is dropped with a note at the next verify |
| `rt check` | Two small Pangram checks and one StealthGPT rewrite: confirms keys, route and billing, whether protected items come through, and whether the rewrite would pass |
| `rt pangram [file \| - \| --text="…"]` | Pangram only: scores the text (a file, stdin, or inline) and says whether it would pass. No spec, no stamps |
| `rt stealth [file \| - \| --text="…"]` | StealthGPT only: one rewrite, printed to stdout (billing to stderr). Paths, numbers and code are protected as in `rt run` and a warning names any that changed |
| `rt baseline [file …]` | Stamps files as they are, without revising them: the named files (replacing their stamps, e.g. to accept a hand edit), or else every target file never stamped |

In a Claude session, `/revise-text:check <file | text>` runs `rt pangram` and `/revise-text:rewrite <file | text>`
runs `rt stealth`, with text typed after the command passed as is.

For one-off use, ask in the prompt: "Write the README, then run revise-text on the Overview and Motivation
sections only."

## The spec

Saved as `.revise-text/specs/<path with / replaced by -->.json`, e.g. `tasks--017--instruction.md.json` for
`tasks/017/instruction.md`:

```json
{
  "regions": [{"id": "whole", "select": "whole"}],
  "requirements": [
    "R1: the logs are in `/data/logs`",
    "R2: write `/out/summary.csv` with columns `ts`, `level`, `count`",
    "R3: count only `ERROR` and `WARN` lines",
    "R4: at most 200 rows, newest first"
  ],
  "word_range": [150, 200]
}
```

| `select` | Revises |
|---|---|
| `"whole"` | Everything after the frontmatter: YAML between `---` and `---` or `...` (starting with a key or a comment on the line after the opening `---`, and made of YAML-looking lines, blank
lines allowed; a `---` followed by a blank line is a thematic break), or TOML between `+++` lines |
| `"## Overview"` | That section, up to the next heading of the same or higher level. Headings are found the way CommonMark reads them (with the bundled markdown-it-py parser): setext (`===`, `---`) headings end a section too, while `#` lines in code, HTML blocks, lists, quotes or the frontmatter don't. Only an ATX (`#`) heading can be selected, and it must appear exactly once; use a marker block otherwise |
| `"marker"` | Text between `<!-- revise:start id=<id> -->` and `<!-- revise:end -->`, each on its own line. Blocks can't nest; marker lines inside code blocks are ignored |

Region ids must be unique and regions must not overlap; `rt` refuses a spec that breaks either rule. `word_range` is the spec's when it has one region;
with several, give a region its own `"word_range"`. `rt run` warns when a draft is outside its range, and rejects
a StealthGPT attempt that leaves a draft's range.

Everything outside the regions stays byte-for-byte the same, including a BOM, the frontmatter, CRLF or mixed
line endings, and marker blocks the spec doesn't list. Only the spec's own markers are removed (an empty marker
block is removed with the line break after it). A marker is a one-line HTML comment, and it ends a paragraph,
list or quote; where removing it would let the region and the text beside it run together, `rt` leaves a blank
line, or an empty `<!-- -->` (next to lists, quotes or indented code), whichever is the least it needs for the
document to render exactly as it did. A
file with unbalanced or nested
markers is refused, and so is a region that cuts through a marker block.

Always protected: code blocks (fenced or indented, also inside lists and quotes), inline code (including spans
that wrap onto the next line and ``` ``double`` ``` spans), HTML comments, tags and raw blocks, URLs,
absolute, `~/` and relative paths, directories written with a trailing slash (`build/`), but not prose pairs
like "and/or" or "read/write", file names with common extensions, whole links and images, anything in square brackets (reference links,
footnotes, task boxes) and `[label]: url` lines, numbers with their
sign, currency (any symbol, before or after), `%` or unit (`-5`, `$20`, `500€`, `50%`, `512MB`), identifiers with digits (`v1.2`, `sha256`), headings, tables (with or without outer
pipes), `<pre>`/`<script>`/`<style>` blocks, marker blocks the spec doesn't list, anything in backticks
in the requirements, and anything listed in `exact`. A single lowercase word in backticks in a requirement
(`` `count` ``) is protected in its backticked form only, so the same word in ordinary prose can still be
rewritten; backtick it in the text too when it names a field. Protected items go to StealthGPT as `[[E0]]`-style
tokens using its `preservation` mode (overlapping items share one token).

After each attempt, `rt` finds the protected items in the output with the same rules and rejects the attempt
unless they are all there and unchanged, and no letter, digit or sign-like character appeared, disappeared or
changed right next to one (`~200`, `200+`, `200k`, `v1.2b`, `-5` → `5`; a footnote or link after a reworded word
(`runtime[^2]`) is fine, as are emphasis around an item (`*200*`, `_200_`), a slash joining two items (`` `ERROR`/`WARN` ``) and a
trailing hyphen as in "200-row"). Through the REST API they must also stay in the same order, which its
preservation mode guarantees; StealthGPT's MCP tool restructures sentences freely, so through MCP items may move
between sentences, but block items (headings, tables, code blocks, `[label]: url` lines, unlisted marker
blocks) must keep their order and every other item must stay between the same two block items. A value that ends up attached to the wrong thing (source and destination swapped) is for the
meaning check to catch. An item that had a line to itself (a heading, table, code block) must
keep its indentation and stay alone on its line, and a blank line next to it may only go if the document renders
the same without it (one after a heading or before a code fence can go; one between a table and the next
paragraph can't). An item the rewriter wrote out
itself instead of using its token is fine; one it added (say, a new number) is not. Words aren't items: "at most"
becoming "at least" is for the meaning check to catch, which is why limits belong in backticks in requirements.

## Settings (`.revise-text/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `targets` | `[]` | Glob patterns for files covered by the finish check and `rt status` |
| `enforce` | `false` | Turn on the finish check |
| `pass_threshold` | `0.5` | Pass when Pangram's `fraction_ai` is below this |
| `check_draft` | `true` | Check Claude's draft with Pangram before calling StealthGPT |
| `stealth_attempts` | `2` | StealthGPT attempts per run. Through the REST API attempt 2 uses `preservationMode: split`; through MCP the request is the same and only StealthGPT's output varies |
| `retries` | `1` | `rt retry` calls allowed per region |
| `length_ratio` | `[0.8, 1.25]` | Reject an attempt whose word count drifts outside this ratio |
| `min_words` | `50` | Shorter regions are marked SHORT and left alone. Placeholder: Pangram doesn't document a minimum |
| `log_text` | `true` | Keep before and after text in the history (needed to export failures as test cases) |
| `pangram_model` | `"pangram-4.1"` | Pangram model selector (your key lists `default`, `pangram-4` and `pangram-4.1`; use whichever your text is checked with) |
| `stealth_model` | `"super"` | StealthGPT model |
| `stealth_via` | `"mcp"` | `"mcp"`: StealthGPT's MCP server (billed to your subscription when MCP billing is set to it). `"api"`: the REST API (always API credits) |

**APIs used.** Pangram: `POST https://text.external-api.pangram.com/task`, then poll `GET /task/{id}`; reads
`fraction_ai`, `fraction_ai_assisted` and each window's `is_humanized`. StealthGPT:
through MCP, `humanize_text` and `get_run_status` on `https://www.stealthgpt.ai/api/mcp/mcp` (Bearer token,
Streamable HTTP), with `model`, `outputFormat: "text"` (plain text) and `qualityMode: "quality"`. As of 2026-10-26 the tool has no
preservation option (`rt` would send one if the tool's input schema ever lists it; `rt check` shows the inputs),
but in live tests it kept the `[[E0]]` tokens, moving them with the sentences.
Through the REST API, `POST https://www.stealthgpt.ai/api/stealthify` with `rephrase: true`,
`preservation: true` and `outputFormat: "text"` (plain text). Words billed to API credits cost $0.05 per 100 (super, input
plus output) and go into `stealth_cost_usd`; words billed to the subscription go into
`stealth_subscription_words`, and words whose billing StealthGPT didn't report or named in a way `rt` doesn't
know (or a request that may have started a run and then failed) into `stealth_unknown_billing_words`. Metered
credits StealthGPT reports on a subscription run (`meteredChargedCredits`) are added to the dollar cost.

## Run history

`rt` appends to `~/.revise-text/history.jsonl`: one `run` record per region per run or retry, and one `verify`
record per region per verification, joined by `run_id`. A retry has `mode: "retry"` and `retry_of`. Records
include the plugin version and the bundled humanize-writing commit. API keys are never written. Move old
months into `history-YYYY-MM.jsonl`; the dashboard and insights read every `history*.jsonl` file.

**`/revise-text:dashboard`** builds `~/.revise-text/dashboard.html` (offline, light or dark) with summary tiles,
where regions pass week by week, Pangram before and after, meaning checks, breakdowns by mode, input and
version, rejection reasons, frequent critique points, cost, and a filterable runs table with details.

## Using the history to update the plugin

Run `/revise-text:insights` (optionally `--since 2026-10-01`) every 50–100 regions, after a humanize-writing
update, and whenever the dashboard shows a drop. It writes `insights.md` with suggestions. Nothing changes
automatically.

| Finding in the history | Suggested change |
|---|---|
| The meaning check keeps failing on one kind of requirement (limits, counts, conditions) | Backtick those phrases in requirements so they're protected, or add a rule to `overrides.md` |
| A critique point appears in many runs | Add a rule to `overrides.md` so the first draft avoids it |
| StealthGPT often loses or adds one kind of exact item (`lost_item_kinds`, `added_item_kinds`) | Add or adjust a pattern in `rt.py` |
| One kind of change rejects most attempts (`item_change_kinds`: signs, blocks moved, sections) | Check examples in the dashboard; if the rule rejects harmless rewrites, adjust `significant()` or the block rules in `rt.py` |
| Length is often the rejection reason | Adjust `length_ratio` |
| The draft almost never passes Pangram (`draft_pass_rate` under 5%) | Set `check_draft: false` to save one check per region |
| Attempt 2 rarely passes when attempt 1 failed | Set `stealth_attempts: 1` |
| Retries rarely fix a mismatch (`retry_fixed_mismatch`) | Set `retries: 0` and go straight to `--final` |
| Many regions are SHORT (`short_share`) | Merge small regions in the spec, or lower `min_words` |
| Pass rate drops after a humanize-writing update (`pass_rate_by_version`) | Return to the previous pinned commit |
| Pass rate drops with no plugin change (`pass_rate_by_day`) | Pangram has probably updated its detector; check `pangram_model` and `pangram_humanized_flag_share` |

`--export-cases` writes REVIEW, ERROR and mismatched regions (where text was logged) into `tests/cases/` of a
plugin checkout, or `./revise-text-cases/` otherwise.

To apply a change: accept suggestions, make the edits, run the regression test on the old and new versions,
and if the new one is no worse, raise the version in `.claude-plugin/plugin.json` and add a `CHANGELOG.md`
entry naming the findings.

## Tests

```
python3 -m unittest discover tests            # rt.py with mocked APIs; no keys, no cost
python3 tests/regress.py --judge --out=new.json   # real APIs on tests/cases; costs money
python3 tests/regress.py --compare=old.json,new.json
```

`regress.py --redraft` writes fresh drafts from each case's requirements with `claude -p` and this checkout's
writing rules. Use it to compare humanize-writing versions. `--plain` is the baseline to compare Mode A
against: a draft rewritten with every humanize-writing pass, one Pangram check, no StealthGPT. Cases with
`"human": true` are human-written controls, only checked with Pangram, to track false positives. A useful set
is 10 of your own tasks plus 5 human-written controls; `tests/cases/` ships with one example task, and the
controls have to be text a person actually wrote.

## Bundled humanize-writing

`skills/revise-text/references/humanize-writing/` is a pinned copy of
[jpeggdev/humanize-writing](https://github.com/jpeggdev/humanize-writing) (MIT), read as reference files rather
than installed as a skill. Its `SKILL.md` is stored as `humanize-writing.md` so no skill loader picks it up as a
second skill. Never edit the copy; your own rules go in `references/overrides.md`.

Every Monday, `.github/workflows/check-upstream.yml` runs `update_vendor.py --apply` and opens a pull request if
upstream has moved. Before merging, check for removed files, rules that clash with `overrides.md` and license
changes, and run the regression test on both versions.

## Limits

- Claude's drafts rarely pass Pangram, so expect most regions to reach StealthGPT.
- Pangram 4 has a humanizer detector (`is_humanized` per window). The history records how often it fires
  (`humanized_windows`); passing is decided on `fraction_ai` alone.
- Verification is only as good as the spec. Write limits and conditions precisely and backtick exact strings.
  Plain-word field names aren't caught by the automatic patterns; backtick them in the requirements and the text.
- Spec names replace `/` with `--`, so `a/b.md` and a file literally named `a--b.md` would share a spec.
- `min_words` is a placeholder. Both APIs were tested live on 2026-10-26 (Pangram 4.1; StealthGPT super through
  MCP, billed to the subscription); run `rt check` after any change on their side.
- When StealthGPT changes a protected item, the attempt is rejected (it still uses plan words); the file is never
  affected, since every protected item is checked. The dashboard's rejection reasons show how often, by kind.
- StealthGPT can keep every protected item and still scramble how they fit together (in a live test: "limit the
  results to a maximum of /out/summary.csv 200"). Only the meaning check catches that, so expect some regions to
  end as REVIEW with Claude's draft kept, especially short, technical ones.
- Check Pangram's and StealthGPT's terms, and the rules of wherever the text ends up, before running at volume.
