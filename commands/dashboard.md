---
description: Build and open the revise-text dashboard from your run history
allowed-tools: Bash(python3:*)
---
Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/revise-text/scripts/dashboard.py"` and tell the user how many records it
read and where the page is. If there is no history yet, say that the first `rt run` will create it.
