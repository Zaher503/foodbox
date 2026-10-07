"""Recipe Box backend: job queue + JSON API + static UI. Run: uvicorn main:app --port 8800"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import fetch

APP = Path(__file__).parent
BASE = Path("/srv/recipebox")
RECIPES, TRASH = BASE / "recipes", BASE / "trash"
KEY = os.environ["RECIPE_KEY"]
MEALS = ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Other"]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
SERVE_EXT = IMG_EXT | {".mp4", ".html", ".txt", ".vtt"}
URL_RE = re.compile(r"https?://[^\s\"'<>]+")

for p in (RECIPES, TRASH):
    p.mkdir(parents=True, exist_ok=True)

db = sqlite3.connect(BASE / "jobs.sqlite", check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, meal TEXT, recipe_id TEXT,
  status TEXT, error TEXT, created REAL, updated REAL)""")
db.execute("UPDATE jobs SET status='queued' WHERE status='running'")  # resume after restart
db.commit()
dblock = threading.Lock()


def q(sql, *a):
    with dblock:
        cur = db.execute(sql, a)
        db.commit()
        return cur


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------- recipes on disk ----------
def rdir(rid: str) -> Path:
    if not re.fullmatch(r"[\w.\-]+", rid):
        raise HTTPException(404)
    d = RECIPES / rid
    if not (d / "recipe.json").exists():
        raise HTTPException(404)
    return d


def load(d: Path) -> dict:
    r = json.loads((d / "recipe.json").read_text())
    r["id"] = d.name
    r["photos"] = sorted(f.name for f in (d / "shots").glob("*") if f.suffix.lower() in IMG_EXT) \
        if (d / "shots").exists() else []
    r["has_page"] = (d / "page.html").exists()
    r["has_thumb"] = (d / "thumbnail.jpg").exists()
    return r


def save(d: Path, r: dict):
    r = {k: v for k, v in r.items() if k not in ("id", "photos", "has_page", "has_thumb")}
    tmp = d / "recipe.json.tmp"
    tmp.write_text(json.dumps(r, indent=2, ensure_ascii=False))
    tmp.replace(d / "recipe.json")


def all_recipes():
    out = []
    for d in RECIPES.iterdir():
        if (d / "recipe.json").exists():
            try:
                out.append(load(d))
            except Exception:
                pass
    return out


def find_by_url(url):
    for r in all_recipes():
        if r.get("source_url") == url:
            return r["id"]


def write_stub(d: Path, url: str, err: str):
    fx = json.loads((d / "fetch.json").read_text()) if (d / "fetch.json").exists() else {}
    save(d, {"title": fx.get("yt_title") or url, "meal": "Other", "servings": None,
             "ingredients": [], "steps": [], "source_url": url, "kind": fx.get("kind", "video"),
             "cover": "thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None,
             "has_video": (d / "source.mp4").exists(),
             "needs_review": f"failed: {err[:200]}", "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})


# ---------- worker ----------
def unload_model():
    try:
        requests.post("http://127.0.0.1:11434/api/generate",
                      json={"model": "qwen2.5vl:7b", "keep_alive": 0}, timeout=20)
    except Exception:
        pass


def run_job(job):
    url, d = job["url"], None
    try:
        if job["recipe_id"]:
            d = RECIPES / job["recipe_id"]
        else:
            unload_model()
            d = fetch.fetch(url)
        unload_model()
        keep_user = {}
        if job["recipe_id"]:
            try:
                old = json.loads((d / "recipe.json").read_text())
                keep_user = {k: old[k] for k in ("favorite", "notes", "tags") if k in old}
            except Exception:
                pass
            (d / "recipe.json").unlink(missing_ok=True)
        p = subprocess.run([sys.executable, "process.py", str(d)], cwd=APP,
                           capture_output=True, text=True, timeout=1800)
        if not (d / "recipe.json").exists():
            raise RuntimeError((p.stdout + p.stderr).strip().splitlines()[-1][:200] if (p.stdout + p.stderr).strip()
                               else "processing produced nothing")
        if keep_user:
            r = load(d)
            r.update(keep_user)
            save(d, r)
        if job["meal"] in MEALS:
            r = load(d)
            r["meal"] = job["meal"]
            save(d, r)
        q("UPDATE jobs SET status='done', recipe_id=?, updated=? WHERE id=?", d.name, time.time(), job["id"])
    except Exception as e:
        err = (getattr(e, "stderr", "") or "") if isinstance(getattr(e, "stderr", ""), str) else ""
        msg = (str(e) + " " + err).strip()
        m = re.search(r"Error\('(.+?)'\)", msg)
        msg = (m.group(1) if m else msg)[:300]
        if "instagram.com" in url:
            msg = "Instagram links are not supported yet; link saved"
        rid = None
        try:
            if d is None:
                d = fetch.make_dir(url, re.sub(r"\W+", "-", fetch.urlparse(url).netloc))
            if not (d / "recipe.json").exists():
                write_stub(d, url, msg)
            rid = d.name
        except Exception:
            pass
        q("UPDATE jobs SET status='failed', error=?, recipe_id=?, updated=? WHERE id=?", msg, rid, time.time(), job["id"])


def worker():
    while True:
        with dblock:
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (time.time(), row["id"]))
                db.commit()
        if row:
            run_job(dict(row))
        else:
            time.sleep(2)


threading.Thread(target=worker, daemon=True).start()

# ---------- add ----------
hits: list = []


def enqueue(raw: str, meal=None, recipe_id=None):
    m = URL_RE.search(raw or "")
    if not m:
        raise HTTPException(400, "no link found")
    url = m.group(0).rstrip(".,)")
    try:
        fetch.check_url(url)
    except Exception as e:
        raise HTTPException(400, str(e))
    if not recipe_id:
        rid = find_by_url(url)
        if rid:
            return {"status": "exists", "recipe_id": rid}
        j = q("SELECT id FROM jobs WHERE url=? AND status IN ('queued','running')", url).fetchone()
        if j:
            return {"status": "queued", "job": j["id"]}
    cur = q("INSERT INTO jobs(url,meal,recipe_id,status,created,updated) VALUES(?,?,?,?,?,?)",
            url, meal, recipe_id, "queued", time.time(), time.time())
    return {"status": "queued", "job": cur.lastrowid}


def throttle():
    now = time.time()
    hits[:] = [t for t in hits if now - t < 60]
    if len(hits) >= 30:
        raise HTTPException(429, "slow down")
    hits.append(now)


@app.post("/api/add")
def api_add(body: dict = Body(...), x_recipe_key: str = Header(default="")):
    throttle()
    if not secrets.compare_digest(x_recipe_key, KEY):
        raise HTTPException(401, "bad key")
    return enqueue(body.get("url") or body.get("text") or "", body.get("meal"))


@app.post("/api/ui/add")  # used by the web page; Authentik protects it at the proxy
def ui_add(body: dict = Body(...)):
    throttle()
    return enqueue(body.get("url", ""), body.get("meal"))


@app.get("/api/jobs")
def jobs():
    rows = q("SELECT id,url,status,error,recipe_id FROM jobs WHERE status IN ('queued','running') "
             "OR (status='failed' AND updated > ?) ORDER BY id DESC LIMIT 30", time.time() - 86400).fetchall()
    return [dict(r) for r in rows]


# ---------- browse / edit ----------
@app.get("/api/recipes")
def recipes(meal: str = "", q_: str = Query("", alias="q"), sort: str = "new", tag: str = ""):
    rs = all_recipes()
    if meal == "Review":
        rs = [r for r in rs if r.get("needs_review")]
    elif meal == "Favorites":
        rs = [r for r in rs if r.get("favorite")]
    elif meal:
        rs = [r for r in rs if r.get("meal") == meal]
    tag_counts = {}
    for r in rs:
        for t in r.get("tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    want = [t.strip().lower() for t in tag.split(",") if t.strip()]
    if want:
        rs = [r for r in rs if all(t in (r.get("tags") or []) for t in want)]
    needle = q_.lower().strip()
    if needle:
        rs = [r for r in rs if needle in json.dumps([r.get("title"), r.get("ingredients"), r.get("steps"),
                                                     r.get("tags"), r.get("notes")]).lower()]
    rs.sort(key=(lambda r: (r.get("title") or "").lower()) if sort == "az"
            else (lambda r: r.get("processed_at") or ""), reverse=(sort != "az"))
    keep = ("id", "title", "meal", "cover", "needs_review", "kind", "has_video", "source_url", "tags", "favorite")
    counts = {m: 0 for m in MEALS}
    review = favs = 0
    all_tags = set()
    for r in all_recipes():
        counts[r.get("meal") if r.get("meal") in MEALS else "Other"] += 1
        review += bool(r.get("needs_review"))
        favs += bool(r.get("favorite"))
        all_tags.update(r.get("tags") or [])
    return {"recipes": [{k: r.get(k) for k in keep} for r in rs], "counts": counts, "review": review,
            "favorites": favs, "all_tags": sorted(all_tags),
            "tags": [{"tag": t, "n": n} for t, n in sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))]}


@app.get("/api/recipes/{rid}")
def recipe(rid: str):
    return load(rdir(rid))


@app.patch("/api/recipes/{rid}")
def patch(rid: str, body: dict = Body(...)):
    d = rdir(rid)
    r = load(d)
    if "meal" in body:
        if body["meal"] not in MEALS:
            raise HTTPException(400, "bad meal")
        r["meal"] = body["meal"]
    for k in ("title", "servings"):
        if k in body:
            r[k] = str(body[k]).strip()[:200] or None
    for k in ("ingredients", "steps"):
        if k in body:
            r[k] = [str(x).strip() for x in body[k] if str(x).strip()][:200]
    if "tags" in body:
        out = []
        for t in body["tags"] or []:
            t = re.sub(r"\s+", " ", str(t).strip().lower())[:24]
            if t and t not in out:
                out.append(t)
        r["tags"] = out[:12]
    if "favorite" in body:
        r["favorite"] = bool(body["favorite"])
    if "notes" in body:
        r["notes"] = str(body["notes"] or "")[:5000]
    for k in ("prep_min", "cook_min", "total_min"):
        if k in body:
            try:
                v = int(float(body[k]))
            except (TypeError, ValueError):
                v = None
            if v is not None and not 1 <= v <= 2880:
                raise HTTPException(400, "bad time")
            r[k] = v
    if "servings_num" in body:
        try:
            n = int(float(body["servings_num"]))
        except (TypeError, ValueError):
            n = None
        if n is not None and not 1 <= n <= 100:
            raise HTTPException(400, "bad servings number")
        r["servings_num"], r["servings_inferred"] = n, False
    if "nutrition" in body:
        nut = body["nutrition"]
        if nut is None:
            r["nutrition"] = None
        elif isinstance(nut, dict):
            clean = {str(k).strip()[:40]: str(v).strip()[:40] for k, v in nut.items()
                     if str(k).strip() and str(v).strip()}
            r["nutrition"] = dict(list(clean.items())[:20]) or None
        else:
            raise HTTPException(400, "bad nutrition")
    if "cover" in body:
        c = body["cover"]
        ok = c == "thumbnail.jpg" or (c.startswith("shots/") and (d / c).resolve().parent == (d / "shots").resolve())
        if not ok or not (d / c).exists():
            raise HTTPException(400, "bad cover")
        r["cover"] = c
    if body.get("reviewed"):
        r["needs_review"] = None
    save(d, r)
    return load(d)


@app.delete("/api/recipes/{rid}")
def delete(rid: str):
    d = rdir(rid)
    shutil.move(str(d), str(TRASH / f"{d.name}_{int(time.time())}"))
    return {"ok": True}


@app.post("/api/recipes/{rid}/reprocess")
def reprocess(rid: str):
    d = rdir(rid)
    r = load(d)
    if not (d / "fetch.json").exists():
        return enqueue(r["source_url"])
    return enqueue(r["source_url"], recipe_id=d.name)


@app.post("/api/recipes/{rid}/shots")
async def add_shot(rid: str, file: UploadFile = File(...)):
    d = rdir(rid)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in IMG_EXT:
        raise HTTPException(400, "images only")
    data = await file.read(15 * 1024 * 1024 + 1)
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "too large")
    (d / "shots").mkdir(exist_ok=True)
    name = f"user_{int(time.time() * 1000)}{ext}"
    (d / "shots" / name).write_bytes(data)
    return {"name": name}


@app.delete("/api/recipes/{rid}/shots/{name}")
def del_shot(rid: str, name: str):
    d = rdir(rid)
    f = d / "shots" / name
    if f.resolve().parent != (d / "shots").resolve() or not f.exists():
        raise HTTPException(404)
    f.unlink()
    r = load(d)
    if r.get("cover") == f"shots/{name}":
        r["cover"] = "thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None
        save(d, r)
    return {"ok": True}


@app.get("/files/{rid}/{path:path}")
def files(rid: str, path: str):
    d = rdir(rid)
    f = (d / path).resolve()
    if d.resolve() not in f.parents or not f.is_file() or f.suffix.lower() not in SERVE_EXT:
        raise HTTPException(404)
    h = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    if f.suffix.lower() == ".html":  # saved third-party page: never let it run scripts on our origin
        h["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data: https:; style-src 'unsafe-inline'"
    return FileResponse(f, headers=h)


ICON_FILES = {"icon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png"}


@app.get("/icons/{name}")
def icon(name: str):
    f = APP / "static" / "icons" / name
    if name not in ICON_FILES or not f.exists():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(APP / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(APP / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def health():
    return JSONResponse({"ok": True})
