---
description: Turn the revise-text run history into suggested changes (nothing changes automatically)
argument-hint: "[--since YYYY-MM-DD] [--export-cases[=DIR]]"
allowed-tools: Bash(python3:*), Read, Write
---
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/revise-text/scripts/insights.py" $ARGUMENTS`.
2. Read `${CLAUDE_PLUGIN_ROOT}/skills/revise-text/references/overrides.md` (the user's own writing rules).
3. Match the numbers against this table:

   | Finding | Suggested change |
   |---|---|
   | `mismatches` keep naming one kind of requirement (limits, counts, conditions) | Backtick those phrases in requirements so they're protected, or add a rule to overrides.md |
   | A `top_critique_points` entry appears in many regions | Add a rule for it to overrides.md |
   | `lost_item_kinds` or `added_item_kinds` is dominated by one kind | Adjust that pattern in rt.py |
   | `item_change_kinds` is dominated by one kind (signs, blocks moved, sections) | Look at examples in the dashboard's runs; if the rule rejects harmless rewrites, adjust `significant()` or the block rules in rt.py |
   | `rejections` are mostly "length" | Adjust `length_ratio` |
   | `draft_pass_rate` under 0.05 | Set `check_draft: false` (saves one Pangram check per region) |
   | `stealth_attempt_pass_rate.stealth_2` is low | Set `stealth_attempts: 1` |
   | `retry_fixed_mismatch` is low | Set `retries: 0` and go straight to `--final` |
   | `short_share` is high | Merge small regions in the spec, or lower `min_words` |
   | `pass_rate_by_version` dropped after a humanize-writing update | Return to the previous pinned commit |
   | `pass_rate_by_day` dropped with no plugin change | Pangram probably updated its detector; check `pangram_model` and `pangram_humanized_flag_share` |
   | `billing_modes` shows `prepaid`/`payg` while `stealth_via` is `mcp` | MCP runs are billed to API credits; switch "MCP billing" to the subscription in StealthGPT's usage settings |

4. Write `insights.md` in the current folder: a short numbered list of suggested changes. For each, give the
   finding with its numbers, the change (exact config value, or the rule text for overrides.md), and how sure
   you are given the sample size. Skip findings with fewer than 10 regions behind them; say so instead.
   Group recurring critique points and mismatch texts into themes before suggesting rules.
5. Don't edit any file other than insights.md. Tell the user to pick what to accept.
