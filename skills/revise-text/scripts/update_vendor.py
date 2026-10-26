"""Check jpeggdev/humanize-writing for updates, show what changed, and apply with --apply."""
import datetime, json, pathlib, shutil, subprocess, sys, tempfile

DEST = pathlib.Path(__file__).resolve().parent.parent / "references" / "humanize-writing"
# upstream path -> bundled name; SKILL.md is renamed so it is never loaded as a second skill
FILES = {"SKILL.md": "humanize-writing.md", "references/ai-tells.md": "ai-tells.md", "LICENSE": "LICENSE"}

pin = json.loads((DEST / "UPSTREAM.json").read_text())
latest = subprocess.check_output(["git", "ls-remote", pin["repo"], "HEAD"], text=True).split()[0]
if latest == pin["commit"]:
    print("up to date"); sys.exit()

tmp = pathlib.Path(tempfile.mkdtemp())
try:
    subprocess.run(["git", "clone", "--quiet", "--depth", "1", pin["repo"], str(tmp)], check=True)
    for src, dst in FILES.items():
        if not (tmp / src).exists():
            sys.exit(f"upstream no longer has {src}; review manually")
        subprocess.run(["git", "--no-pager", "diff", "--no-index", "--stat", str(DEST / dst), str(tmp / src)])
        subprocess.run(["git", "--no-pager", "diff", "--no-index", str(DEST / dst), str(tmp / src)])
    if "MIT License" not in (tmp / "LICENSE").read_text():
        print("WARNING: upstream LICENSE is no longer MIT; check it before bundling")

    if "--apply" in sys.argv:
        for src, dst in FILES.items():
            shutil.copy(tmp / src, DEST / dst)
        pin.update(commit=latest, updated=datetime.date.today().isoformat())
        (DEST / "UPSTREAM.json").write_text(json.dumps(pin, indent=2) + "\n")
        print(f"updated to {latest}; run the regression test before releasing")
    else:
        print(f"upstream is at {latest}; run with --apply to take it")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
