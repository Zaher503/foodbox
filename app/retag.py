"""Re-tag every saved recipe with the small tag vocabulary (protein, appliance, cuisine).
Replaces existing tags, including hand-edited ones. Run inside the container while the app is idle:
docker exec foodbox-app-1 python /app/app/retag.py"""
import json
import os
from pathlib import Path

import process as P

BASE = Path(os.environ.get("FOODBOX_DATA") or "/data") / "recipes"

for d in sorted(BASE.iterdir()):
    rj = d / "recipe.json"
    if not rj.exists():
        continue
    r = json.loads(rj.read_text(encoding="utf-8"))
    before = r.get("tags") or []
    r["tags"] = P.gen_tags(r)
    rj.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    print(d.name[:45], before, "->", r["tags"], flush=True)
