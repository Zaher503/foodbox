"""One-off: add nutrition + servings_num to recipes saved before those fields existed.
Run from ~/recipebox/app with the venv python while the app is idle."""
import json
import sys
from pathlib import Path

import process as P

for d in sorted(Path("/srv/recipebox/recipes").iterdir()):
    rj = d / "recipe.json"
    if not rj.exists():
        continue
    r = json.loads(rj.read_text(encoding="utf-8"))
    if "servings_num" in r and "nutrition" in r:
        continue
    nut, corpus = None, ""
    if (d / "jsonld.json").exists():
        try:
            nut = P.nutrition_from_ld(json.loads((d / "jsonld.json").read_text(encoding="utf-8")) or {})
        except Exception:
            nut = None
    if not nut:
        if (d / "page.txt").exists():
            corpus = (d / "page.txt").read_text(encoding="utf-8")
        elif (d / "page.html").exists():
            corpus = P.page_text((d / "page.html").read_text(errors="replace", encoding="utf-8"))
        else:
            parts = []
            fj = d / "fetch.json"
            if fj.exists():
                parts.append(json.loads(fj.read_text(encoding="utf-8")).get("caption", ""))
            if (d / "transcript.txt").exists():
                parts.append((d / "transcript.txt").read_text(encoding="utf-8"))
            if (d / "ocr.json").exists():
                parts += [s["text"] for s in json.loads((d / "ocr.json").read_text(encoding="utf-8"))]
            corpus = " ".join(parts)
        nut = P.extract_nutrition(corpus)
    r["nutrition"] = nut
    P.add_servings(r)
    rj.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    print(d.name, "| nutrition:", bool(nut), "| servings:", r["servings"], "->", r["servings_num"],
          "(estimated)" if r["servings_inferred"] else "", flush=True)
