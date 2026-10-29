# Changelog

## Unreleased

- `rt pangram` and `rt stealth`: call one API on its own, with the text inline (`--text="…"`), in a file, or on
  stdin. `rt stealth` protects exact items like `rt run`. In a session they are
  `/revise-text:check` and `/revise-text:rewrite`.
- StealthGPT is asked for plain text (`outputFormat: "text"`) instead of Markdown, on both the REST and MCP routes.

- `rt run`: with several regions, the "draft is missing these spec items" warning checks the whole file, so
  items that belong to another region no longer show up as missing in every region.

## 0.1.0 — 2026-10-26

- First version: `rt` run / verify / retry / backup / status / baseline / guard, Pangram 4 check, StealthGPT
  super fallback with preservation tokens, meaning check against a spec, finish check (Stop hook).
- StealthGPT through its MCP server by default (`stealth_via: "mcp"`), so runs can be billed to the web
  subscription; `rt check` confirms the setup. Words billed to the subscription and to API credits are logged apart.
- Dashboard and insights from `~/.revise-text/history*.jsonl`; regression runner in `tests/regress.py`.
- Bundled humanize-writing at da03340, and markdown-it-py 3.0.0 with mdurl 0.1.2 for CommonMark block structure.
