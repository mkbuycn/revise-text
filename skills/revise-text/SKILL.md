---
name: revise-text
description: Revise Markdown you generated (the whole file, a section, or marked blocks) or text the user gives
  you so it reads as naturally written, check it with Pangram, fall back to StealthGPT, and verify it still says
  what it must. Use when a project's CLAUDE.md or .revise-text/config.json makes a file a revise-text target,
  when asked to humanize text AND check it against Pangram, or when the user types /revise-text.
---
`rt` means `python3 "${CLAUDE_SKILL_DIR}/scripts/rt.py"`, run from the project folder. Never print API keys.
Never invent names, numbers, dates, quotes or citations. Never edit text that rt produced: change the spec or
your draft and run again. After verify the spec's markers are gone, so wrap the text in markers again (or select
it by heading). Your draft for a region is in .revise-text/work/<key>/<region>/result.json until the next run,
and every run backs up the whole file in .revise-text/backups/.

## Mode A: content you are generating (the usual case)
1. While writing each target region, follow references/humanize-writing/ai-tells.md and references/overrides.md.
   Before saving, reread each region once against ai-tells.md, fix what still sounds AI-written, and note those
   points for --critique.
2. Save the file. For part of a file, wrap the part in <!-- revise:start id=<name> --> … <!-- revise:end -->
   (each marker on its own line), or select it by heading in the spec. Save the spec as
   .revise-text/specs/<file path with / as -->.json:
   ```json
   {"regions": [{"id": "whole", "select": "whole"}],
    "requirements": ["R1: the logs are in `/data/logs`", "R2: at most 200 rows, newest first"],
    "word_range": [150, 200]}
   ```
   `select` is "whole" (everything after the frontmatter), a heading such as "## Overview", or "marker".
   Regions can't overlap and marker blocks can't nest. With several regions, put "word_range" on each region.
   Requirements are what the text must convey, one each, with exact strings in backticks. State limits and
   conditions precisely; those are what rewriting softens. Optional "exact": more strings to keep verbatim.
   A single lowercase word in backticks (`count`) is only protected where your text backticks it too.
3. Run `rt run <file> --critique="point; point"`. For each region it prints the result, the requirements and
   the revised text.
4. Compare each region with its requirements. StealthGPT may move values between sentences, so check that each
   number, path and name still belongs to the right thing (a source path is still the source). Run `rt verify <file> <region>=ok …`, or
   `<region>="R3: what changed"` for a mismatch.
   - All ok: rt puts the text back, removes the markers and stamps the file. You're done.
   - A mismatch: run `rt retry <file> <region>` once, then verify again (regions you already marked ok stay
     ok). If it still doesn't match, run `rt verify <file> <region>="…" --final`. This keeps your draft for
     that region, marked REVIEW.
   - "the file changed after rt run": you edited it; run `rt run` again.
5. Report the lines `rt verify` printed, one per region, e.g. `whole: PASS at stealth_1 · Pangram AI 0.08 ·
   verified`. Paste the text only if asked.

## Mode B: a file or text you didn't write
1. Pasted text: save it as .revise-text/paste/<n>.md and use that as the file. Run `rt backup <file>`.
2. Write the spec from the original: every fact and claim becomes a requirement.
3. Rewrite the regions in place with every pass in references/humanize-writing/humanize-writing.md, then
   ai-tells.md and overrides.md. Critique once and fix.
4. Continue with Mode A steps 3–5, using `rt run --standalone`. For pasted text, show the first rewrite, the
   critique and the final text.

## Other commands
- `rt check`: two small Pangram checks and one StealthGPT rewrite, to confirm the keys, the billing (subscription or
  API credits) and that protected items come through. Run it when setting up or when runs start failing.
- `rt status`: every target file and its state (ok, not revised, edited after revising).
- `rt baseline [file …]`: stamp files as they are. With no files, every target never stamped (so the finish
  check ignores older files); with files, re-stamp them, e.g. after the user accepts a hand edit.
- Results: PASS, REVIEW (best version kept, still flagged), SHORT (under min_words; left as is), ERROR (an API
  failed, or the region is over StealthGPT's 3,000 words; your draft is kept). Errors name the cause; if a key is
  missing, tell the user which one to set, and split a region that is too long.
