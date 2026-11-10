"""Try the photo pipeline on sample pages and compare reading modes. Nothing is saved to the library.

Inside the running container (the data folder is /data there, /srv/recipebox on the Zephyrus):
  docker exec -w /app/app foodbox-app-1 python ocr_trial.py /data/ocr-trial/*.jpg
  docker exec -w /app/app foodbox-app-1 python ocr_trial.py --tiles 1,2,3 --model qwen2.5vl:7b /data/ocr-trial/page.jpg

Pass several photos to treat them as the pages of one recipe (in that order).
Each mode writes <out>/tiles-N/ with ocr.txt (what the model read) and recipe.json (the structured result),
and prints a short summary so the modes can be compared side by side.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photos", nargs="+", type=Path)
    ap.add_argument("--tiles", default="1,2", help="reading modes to try: 1 = whole page, 2-4 = strips (default 1,2)")
    ap.add_argument("--model", help="vision model to use instead of FOODBOX_MODEL")
    ap.add_argument("--out", type=Path, help="results folder (default: next to the first photo)")
    a = ap.parse_args()
    if a.model:
        os.environ["FOODBOX_MODEL"] = a.model  # read when process is imported
    import fetch
    import process

    out = a.out or a.photos[0].parent / f"results-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pages = []
        for i, p in enumerate(a.photos, 1):  # same conversion as an upload: upright JPEG, long side 2400
            pages.append(Path(tmp) / f"page_{i}.jpg")
            pages[-1].write_bytes(fetch.to_jpeg(p.read_bytes()))
        print(f"model {process.VL}, {len(pages)} page(s), results in {out}\n")
        for n in [int(x) for x in a.tiles.split(",") if x.strip()]:
            t0 = time.time()
            scan = process.read_pages(pages, n, Path(tmp))
            text = "\n\n".join(s["text"] for s in scan if s["text"])
            t_read = time.time() - t0
            d = out / f"tiles-{n}"
            d.mkdir(exist_ok=True)
            (d / "ocr.txt").write_text(text, encoding="utf-8")
            errs = [s["err"] for s in scan if s.get("err")]
            try:
                r = process.ollama(process.PHOTO_PROMPT.format(meals=process.MEALS, text=text[:12000]),
                                   num_ctx=8192, timeout=300)
                r, dropped = process.finish(r, text, "Scanned recipe", evidence_min=30)
            except Exception as e:
                r, dropped = {"error": str(e)[:200]}, None
            (d / "recipe.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"== tiles={n}: read {len(text.split())} words in {t_read:.0f} s, total {time.time() - t0:.0f} s"
                  + (f", errors: {errs}" if errs else ""))
            print(f"   title: {r.get('title')!r}  servings: {r.get('servings')!r}")
            print(f"   {len(r.get('ingredients') or [])} ingredients, {len(r.get('steps') or [])} steps, "
                  f"removed numbers: {dropped}, review flag: {r.get('needs_review')!r}")
            for line in (r.get("ingredients") or [])[:30]:
                print("   -", line)
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
