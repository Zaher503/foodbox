"""One-off: fix HTML entities (&#39;) and stray "(," in saved recipes, and import the recipe's own notes from page.html.
Usage from the app folder: python backfill3.py [recipes dir] [--write]   (dry run unless --write)"""
import json
import sys
from pathlib import Path

import process as P

args = [a for a in sys.argv[1:] if not a.startswith("--")]
write = "--write" in sys.argv
root = Path(args[0] if args else "/srv/recipebox/recipes")


def clean(text):
    return P.tidy_parens(P.unescape(text))


for d in sorted(root.iterdir()):
    rj = d / "recipe.json"
    if not rj.exists():
        continue
    r = json.loads(rj.read_text(encoding="utf-8"))
    new = dict(r)
    for k in ("title", "servings"):
        if isinstance(r.get(k), str):
            new[k] = clean(r[k])
    for k in ("ingredients", "steps"):
        new[k] = [clean(x) for x in r.get(k) or []]
    if (d / "page.html").exists() and not r.get("recipe_notes"):
        notes = P.extract_notes((d / "page.html").read_text(errors="replace", encoding="utf-8"))
        if notes:
            new["recipe_notes"] = notes
    if new != r:
        changed = [k for k in new if new[k] != r.get(k)]
        print(("fixed" if write else "would fix"), d.name, changed)
        if write:
            rj.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
