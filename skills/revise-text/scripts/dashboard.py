"""Build ~/.revise-text/dashboard.html from every history*.jsonl file and open it."""
import json, os, pathlib, sys, webbrowser
from insights import read_history

home = pathlib.Path(os.path.expanduser("~/.revise-text"))
records = read_history(home)
template = (pathlib.Path(__file__).parent.parent / "assets" / "dashboard.html").read_text(encoding="utf-8")
data = json.dumps(records, ensure_ascii=False).replace("<", "\\u003c")   # no "</script>" or "<!--" in the page
out = home / "dashboard.html"
home.mkdir(parents=True, exist_ok=True)
out.write_text(template.replace("/*__RUNS__*/[]", data), encoding="utf-8")
print(f"{len(records)} records → {out}")
if "--no-open" not in sys.argv:
    webbrowser.open(out.as_uri())
