"""One-off: add tags, prep/cook/total times, favorite and notes to recipes saved before those fields existed.
Run from ~/recipebox/app with the venv python while the app is idle."""
import json
from pathlib import Path

import process as P

for d in sorted(Path("/srv/recipebox/recipes").iterdir()):
    rj = d / "recipe.json"
    if not rj.exists():
        continue
    r = json.loads(rj.read_text(encoding="utf-8"))
    if "tags" in r and "prep_min" in r:
        continue
    ld = {}
    if (d / "jsonld.json").exists():
        try:
            ld = json.loads((d / "jsonld.json").read_text(encoding="utf-8")) or {}
        except Exception:
            ld = {}
    times = {"prep_min": P.iso_minutes(ld.get("prepTime")), "cook_min": P.iso_minutes(ld.get("cookTime")),
             "total_min": P.iso_minutes(ld.get("totalTime"))}
    if not any(times.values()):
        if (d / "page.txt").exists():
            corpus = (d / "page.txt").read_text(encoding="utf-8")
        elif (d / "page.html").exists():
            corpus = P.page_text((d / "page.html").read_text(errors="replace", encoding="utf-8"))
        else:
            parts = []
            if (d / "fetch.json").exists():
                parts.append(json.loads((d / "fetch.json").read_text(encoding="utf-8")).get("caption", ""))
            if (d / "transcript.txt").exists():
                parts.append((d / "transcript.txt").read_text(encoding="utf-8"))
            if (d / "ocr.json").exists():
                parts += [x["text"] for x in json.loads((d / "ocr.json").read_text(encoding="utf-8"))]
            corpus = " ".join(parts)
        times = P.extract_times(corpus)
    r.update(times)
    r["tags"] = P.gen_tags(r)
    r.setdefault("favorite", False)
    r.setdefault("notes", "")
    rj.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    print(d.name[:50], "| tags:", r["tags"], "| times:", times, flush=True)
