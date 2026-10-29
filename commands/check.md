---
description: Score text with Pangram only (a file, or text typed after the command)
argument-hint: "<file> | <text>"
allowed-tools: Bash(python3:*)
---
`rt` is `python3 "${CLAUDE_PLUGIN_ROOT}/skills/revise-text/scripts/rt.py"`, run from the project folder.

Arguments: $ARGUMENTS

1. If the arguments are the path of an existing file, run `rt pangram <file>`. Otherwise treat them as the text
   and pass it on stdin, so no quote or `$` in it is changed:
   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/skills/revise-text/scripts/rt.py" pangram - <<'RT_TEXT'
   <the text, exactly as given>
   RT_TEXT
   ```
   With no arguments, ask the user for a file or the text.
2. Report the line rt printed (AI score, AI-assisted, humanized windows, pass or not). Don't edit any file.
   If a key is missing, tell the user to set `PANGRAM_KEY`.
