"""Recipe Box backend: job queue + JSON API + static UI. Run: uvicorn main:app --port 8800"""
import hashlib
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
BASE = fetch.BASE
RECIPES, TRASH = BASE / "recipes", BASE / "trash"


def _load_key():
    """RECIPE_KEY from the environment or ../.env; created on first run if missing."""
    env = Path(os.environ.get("FOODBOX_ENV_FILE") or APP.parent / ".env")
    if not os.environ.get("RECIPE_KEY") and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "RECIPE_KEY" and v.strip() and v.strip() != "change-me":
                return v.strip()
    if os.environ.get("RECIPE_KEY"):
        return os.environ["RECIPE_KEY"]
    key = secrets.token_urlsafe(32)
    with open(env, "a", encoding="utf-8") as f:
        f.write(f"RECIPE_KEY={key}\n")
    print(f"Generated RECIPE_KEY and saved it to {env}")
    return key


KEY = _load_key()
MEALS = ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Other"]
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
SERVE_EXT = IMG_EXT | {".mp4", ".html", ".txt", ".vtt"}
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
# a pasted link without the scheme ("www.tiktok.com/@a/video/1"): needs www. or a path so plain words with dots do not match
BARE_RE = re.compile(r"(?<![\w@./-])(?:www\.[a-z0-9-]+(?:\.[a-z0-9-]+)*|(?:[a-z0-9-]+\.)+[a-z]{2,})(?:/[^\s\"'<>]*)?", re.I)

for p in (RECIPES, TRASH):
    p.mkdir(parents=True, exist_ok=True)

db = sqlite3.connect(BASE / "jobs.sqlite", check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("""CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, meal TEXT, recipe_id TEXT,
  status TEXT, error TEXT, created REAL, updated REAL)""")
try:
    db.execute("ALTER TABLE jobs ADD COLUMN user TEXT")  # who sent the link (older databases lack it)
except sqlite3.OperationalError:
    pass
for col in ("pasted_text TEXT", "text_hash TEXT"):  # a recipe pasted in directly, no link
    try:
        db.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass
db.execute("UPDATE jobs SET status='queued' WHERE status='running'")  # resume after restart
db.commit()
dblock = threading.Lock()
LIVE: dict = {}  # job id -> {"stage": str, "dir": recipe folder name or None}
STAGES = {  # key -> (percent, label shown in the UI)
    "queued": (0, "Waiting in line"), "fetching": (12, "Fetching the link"),
    "reading": (45, "Reading the page"), "transcribing": (35, "Listening to the video"),
    "scanning": (60, "Reading the screen"), "writing": (80, "Writing the recipe"),
    "finishing": (95, "Final touches"), "starting": (20, "Getting started"),
    "waiting": (30, "Waiting for the AI queue"),
}


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
    r = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
    r["id"] = d.name
    r["photos"] = sorted(f.name for f in (d / "shots").glob("*") if f.suffix.lower() in IMG_EXT) \
        if (d / "shots").exists() else []
    r["has_page"] = (d / "page.html").exists()
    r["has_thumb"] = (d / "thumbnail.jpg").exists()
    r["thumbs"] = sorted(f"thumbs/{f.name}" for f in (d / "thumbs").glob("t_*.jpg")) if (d / "thumbs").exists() else []
    r["has_video"] = (d / "source.mp4").exists() or r.get("has_video", False)
    return r


def save(d: Path, r: dict):
    r = {k: v for k, v in r.items() if k not in ("id", "photos", "has_page", "has_thumb", "thumbs")}
    tmp = d / "recipe.json.tmp"
    tmp.write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / "recipe.json")


def all_recipes():
    out = []
    for d in RECIPES.iterdir():
        if (d / "recipe.json").exists():
            try:
                r = load(d)
                if r.get("draft"):  # unsaved "Write one" recipe: hidden, and cleaned up after a day
                    if time.time() - (d / "recipe.json").stat().st_mtime > 86400:
                        shutil.move(str(d), str(TRASH / f"{d.name}_{int(time.time())}"))
                    continue
                out.append(r)
            except Exception:
                pass
    return out


def find_by_url(url):
    for r in all_recipes():
        if r.get("source_url") == url:
            return r["id"]


def find_by_hash(h):
    for r in all_recipes():
        if r.get("paste_hash") == h:
            return r["id"]


def text_hash(text):
    return hashlib.sha1(re.sub(r"\s+", " ", (text or "").strip().lower()).encode()).hexdigest()


def write_stub(d: Path, url: str, err: str):
    fx = json.loads((d / "fetch.json").read_text(encoding="utf-8")) if (d / "fetch.json").exists() else {}
    is_text = fx.get("kind") == "text"
    save(d, {"title": fx.get("yt_title") or fx.get("page_title") or url or "Untitled", "meal": "Other",
             "servings": None, "ingredients": [], "steps": [], "source_url": fx.get("source_url", url),
             "kind": fx.get("kind", "video"),
             "source_note": "Pasted recipe" if is_text else None,
             "paste_hash": text_hash(fx["pasted_text"]) if is_text else None,
             "cover": "thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None,
             "cover_url": None if (d / "thumbnail.jpg").exists() else fx.get("cover_url"),
             "has_video": (d / "source.mp4").exists(),
             "needs_review": f"failed: {err[:300]}", "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})


# ---------- worker ----------
def unload_model():
    if os.environ.get("WHISPER_URL"):
        return  # the shared AI hub swaps models itself, and another app may be using this one
    try:
        requests.post(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/generate",
                      json={"model": os.environ.get("FOODBOX_MODEL") or os.environ.get("RECIPE_VL") or "gemma4:e4b", "keep_alive": 0}, timeout=20)
    except Exception:
        pass


def explain(e: Exception) -> str:
    """The reason a job failed: the tool's own last ERROR line when there is one, not the long command line."""
    err = getattr(e, "stderr", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()] if isinstance(err, str) else []
    if not lines:
        return str(e)
    line = next((ln for ln in reversed(lines) if ln.startswith("ERROR")), lines[-1])
    return re.sub(r";? ?please report this issue.*", "", line)  # yt-dlp's boilerplate tail


def run_job(job):
    url, d = job["url"], None
    live = LIVE[job["id"]] = {"stage": "fetching", "dir": job["recipe_id"]}
    try:
        if job["recipe_id"]:
            d = RECIPES / job["recipe_id"]
        else:
            unload_model()
            d = fetch.fetch_text(job["pasted_text"]) if job.get("pasted_text") else fetch.fetch(url)
        live["dir"] = d.name
        (d / ".stage").unlink(missing_ok=True)
        live["stage"] = "starting"
        unload_model()
        keep_user = {}
        if job["recipe_id"]:
            try:
                old = json.loads((d / "recipe.json").read_text(encoding="utf-8"))
                keep_user = {k: old[k] for k in ("favorite", "notes", "tags", "added_by") if k in old}
            except Exception:
                pass
            (d / "recipe.json").unlink(missing_ok=True)
        p = subprocess.run([sys.executable, "process.py", str(d)], cwd=APP,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800,
                           env={**os.environ, "PYTHONUTF8": "1"})  # Windows default codec is cp1252
        if not (d / "recipe.json").exists():
            raise RuntimeError((p.stdout + p.stderr).strip().splitlines()[-1][:200] if (p.stdout + p.stderr).strip()
                               else "processing produced nothing")
        if keep_user:
            r = load(d)
            r.update(keep_user)
            save(d, r)
        if job["meal"] in MEALS or job["user"]:
            r = load(d)
            if job["meal"] in MEALS:
                r["meal"] = job["meal"]
            if job["user"]:
                r["added_by"] = job["user"]
            save(d, r)
        q("UPDATE jobs SET status='done', recipe_id=?, updated=? WHERE id=?", d.name, time.time(), job["id"])
    except Exception as e:
        msg = explain(e).strip()
        m = re.search(r"Error\('(.+?)'\)", msg)
        msg = (m.group(1) if m else msg)[:300]
        if "instagram.com" in url:
            msg = "Instagram links are not supported yet; link saved"
        rid = None
        try:
            if d is None:
                d = fetch.make_dir(job["pasted_text"][:50], "pasted-recipe") if job.get("pasted_text") \
                    else fetch.make_dir(url, re.sub(r"\W+", "-", fetch.urlparse(url).netloc))
            if not (d / "recipe.json").exists():
                write_stub(d, url, msg)
            rid = d.name
        except Exception:
            pass
        q("UPDATE jobs SET status='failed', error=?, recipe_id=?, updated=? WHERE id=?", msg, rid, time.time(), job["id"])
    finally:
        LIVE.pop(job["id"], None)
        if d is not None:
            (d / ".stage").unlink(missing_ok=True)


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


def enqueue(raw: str, meal=None, recipe_id=None, user=None):
    # Accept "breakfast", " Lunch " etc. from the Shortcut; unknown values fall back to the model's guess
    meal = next((x for x in MEALS if isinstance(meal, str) and x.lower() == meal.strip().lower()), None)
    user = user.strip()[:40] if isinstance(user, str) and user.strip() else None
    m = URL_RE.search(raw or "")
    if not m:
        b = next((x for x in BARE_RE.finditer(raw or "") if x.group(0).lower().startswith("www.") or "/" in x.group(0)), None)
        if not b:
            raise HTTPException(400, "no link found")
        url = "https://" + b.group(0).rstrip(".,)")
    else:
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
    cur = q("INSERT INTO jobs(url,meal,recipe_id,user,status,created,updated) VALUES(?,?,?,?,?,?,?)",
            url, meal, recipe_id, user, "queued", time.time(), time.time())
    return {"status": "queued", "job": cur.lastrowid}


def enqueue_text(raw: str, meal=None, user=None):
    """A full recipe pasted in directly (ingredients + instructions), no link: the local model structures it."""
    meal = next((x for x in MEALS if isinstance(meal, str) and x.lower() == meal.strip().lower()), None)
    user = user.strip()[:40] if isinstance(user, str) and user.strip() else None
    text = (raw or "").strip()
    if len(text) < 40:
        raise HTTPException(400, "paste the full recipe (ingredients and instructions)")
    if len(text) > 20000:
        raise HTTPException(400, "that's too long to paste directly; try the link or Write one instead")
    h = text_hash(text)
    rid = find_by_hash(h)
    if rid:
        return {"status": "exists", "recipe_id": rid}
    j = q("SELECT id FROM jobs WHERE text_hash=? AND status IN ('queued','running')", h).fetchone()
    if j:
        return {"status": "queued", "job": j["id"]}
    cur = q("INSERT INTO jobs(url,meal,recipe_id,user,status,created,updated,pasted_text,text_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?)", "", meal, None, user, "queued", time.time(), time.time(), text, h)
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
    return enqueue(body.get("url") or body.get("text") or "", body.get("meal"), user=body.get("user"))


@app.post("/api/ui/add")  # used by the web page; Authentik protects it at the proxy
def ui_add(body: dict = Body(...)):
    throttle()
    return enqueue(body.get("url", ""), body.get("meal"))


@app.post("/api/ui/paste")  # a full recipe pasted in (ingredients + instructions), no link
def ui_paste(body: dict = Body(...)):
    throttle()
    return enqueue_text(body.get("text", ""), body.get("meal"))


@app.post("/api/ui/add-many")  # bulk: one link per line, one throttle hit for the whole paste
def ui_add_many(body: dict = Body(...)):
    throttle()
    lines = [x.strip() for x in (body.get("text") or "").splitlines() if x.strip()]
    if len(lines) > 100:
        raise HTTPException(400, "at most 100 links at a time")
    out = {"queued": 0, "exists": 0, "failed": [], "jobs": []}
    seen = set()
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        try:
            r = enqueue(line, body.get("meal"))
        except HTTPException as e:
            out["failed"].append({"line": line[:200], "error": e.detail})
            continue
        if r["status"] == "exists":
            out["exists"] += 1
        else:
            out["queued"] += 1
            out["jobs"].append(r["job"])
    return out


def _preview(name):
    """Title and cover of a recipe folder that is still being processed."""
    d = RECIPES / name
    out = {"title": None, "thumb": None, "site": None}
    try:
        fx = json.loads((d / "fetch.json").read_text(encoding="utf-8"))
        out["title"] = fx.get("yt_title") or fx.get("page_title")
        out["site"] = fx.get("uploader") or fx.get("site")
    except Exception:
        pass
    if (d / "thumbnail.jpg").exists():
        out["thumb"] = f"/files/{name}/thumbnail.jpg"
    return out


@app.post("/api/ui/keep")  # push ingredient lines to the Google Keep grocery list
def ui_keep(body: dict = Body(...)):
    throttle()
    email, token = os.environ.get("KEEP_EMAIL", ""), os.environ.get("KEEP_MASTER_TOKEN", "")
    if not email or not token:
        raise HTTPException(503, "Keep is not configured (KEEP_EMAIL / KEEP_MASTER_TOKEN)")
    items = [re.sub(r"\s+", " ", str(x)).strip()[:300] for x in body.get("items") or []]
    items = [x for x in dict.fromkeys(items) if x][:100]
    if not items:
        raise HTTPException(400, "nothing to add")
    title = os.environ.get("KEEP_LIST", "Groceries")
    try:
        import gkeepapi  # private audited fork, see requirements.txt
        keep = gkeepapi.Keep()
        keep.authenticate(email, token)
        note = next((n for n in keep.find(query=title, archived=False, trashed=False)
                     if isinstance(n, gkeepapi.node.List) and n.title.strip().lower() == title.lower()), None)
        if note is None:
            raise HTTPException(404, f'No Keep list named "{title}"')
        have = {i.text.strip().lower() for i in note.unchecked}
        new = [x for x in items if x.lower() not in have]
        for x in new:
            note.add(x, False)
        keep.sync()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Keep error: {str(e)[:150]}")
    return {"added": len(new), "skipped": len(items) - len(new)}


@app.post("/api/ui/manual")  # blank recipe typed in by hand (recipe book, family card, ...)
def ui_manual(body: dict = Body(default={})):
    throttle()
    meal = body.get("meal") if body.get("meal") in MEALS else "Other"
    rid = time.strftime("%Y-%m-%d") + "_manual_" + secrets.token_hex(3)
    d = RECIPES / rid
    d.mkdir(parents=True)
    save(d, {"title": "", "meal": meal, "servings": None, "ingredients": [], "steps": [],
             "source_url": "", "source_note": "", "kind": "manual", "cover": None, "has_video": False,
             "needs_review": None, "tags": [], "favorite": False, "notes": "", "draft": True,
             "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return {"id": rid}


@app.get("/api/jobs")
def jobs():
    now = time.time()
    rows = q("SELECT id,url,status,error,recipe_id FROM jobs WHERE status IN ('queued','running') "
             "OR (status='failed' AND updated > ?) OR (status='done' AND updated > ?) "
             "ORDER BY id DESC LIMIT 30", now - 86400, now - 300).fetchall()
    out = []
    for r in rows:
        j = dict(r)
        live = LIVE.get(j["id"])
        if j["status"] == "running" and live:
            key = live["stage"]
            if live["dir"]:
                try:
                    key = (RECIPES / live["dir"] / ".stage").read_text(encoding="utf-8").strip() or key
                except Exception:
                    pass
            j["stage"] = key if key in STAGES else "starting"
        else:
            j["stage"] = "queued"
        j["pct"], j["label"] = STAGES[j["stage"]]
        if j["stage"] == "waiting" and live and live.get("dir"):
            try:
                j["label"] += " (" + (RECIPES / live["dir"] / ".stage_note").read_text(encoding="utf-8").strip() + ")"
            except Exception:
                pass
        name = (live or {}).get("dir") or j["recipe_id"]
        if name and j["status"] != "failed":
            j.update(_preview(name))
            if j["status"] == "done":
                try:
                    j["title"] = load(RECIPES / name).get("title") or j["title"]
                except Exception:
                    pass
        out.append(j)
    return out


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
    words = re.findall(r"[\w']+", q_.lower())
    if words:  # every typed word must start a word in the title or tags ("bean" finds "Bean Toast" and "beans")
        def hit(r):
            have = re.findall(r"[\w']+", " ".join([r.get("title") or "", *(r.get("tags") or [])]).lower())
            return all(any(h.startswith(w) for h in have) for w in words)
        rs = [r for r in rs if hit(r)]
    def meal_key(r):  # Breakfast, Lunch, ... then A-Z inside each meal
        mm = r.get("meal") if r.get("meal") in MEALS else "Other"
        return (MEALS.index(mm), (r.get("title") or "").lower())
    if sort == "meal":
        rs.sort(key=meal_key)
    else:
        rs.sort(key=(lambda r: (r.get("title") or "").lower()) if sort == "az"
                else (lambda r: r.get("processed_at") or ""), reverse=(sort != "az"))
    keep = ("id", "title", "meal", "cover", "cover_url", "needs_review", "kind", "has_video", "source_url", "tags", "favorite", "added_by",
            "prep_min", "cook_min", "total_min")
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
    for k in ("title", "servings", "source_note"):
        if k in body:
            r[k] = str(body[k]).strip()[:200] or None
    if "source_url" in body:
        u = str(body["source_url"] or "").strip()
        if u and not re.fullmatch(r"https?://\S+", u, re.I):
            raise HTTPException(400, "the source link must start with http:// or https://")
        r["source_url"] = u[:2000]
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
        ok = c == "thumbnail.jpg" or any(c.startswith(p + "/") and (d / c).resolve().parent == (d / p).resolve()
                                         for p in ("shots", "thumbs"))
        if not ok or not (d / c).exists():
            raise HTTPException(400, "bad cover")
        r["cover"] = c
    if body.get("reviewed"):
        r["needs_review"] = None
        r.pop("unverified", None)
    if body.get("draft") is False:
        r.pop("draft", None)
    save(d, r)
    return load(d)


@app.delete("/api/recipes/{rid}")
def delete(rid: str):
    d = rdir(rid)
    if load(d).get("draft"):  # never-saved draft: just discard it
        shutil.rmtree(d)
        return {"ok": True}
    shutil.move(str(d), str(TRASH / f"{d.name}_{int(time.time())}"))
    return {"ok": True}


@app.post("/api/recipes/{rid}/reprocess")
def reprocess(rid: str):
    d = rdir(rid)
    r = load(d)
    if r.get("kind") == "text":
        # pasted recipe: fetch.json already holds the text, just re-run the model on it
        cur = q("INSERT INTO jobs(url,meal,recipe_id,user,status,created,updated) VALUES(?,?,?,?,?,?,?)",
                "", None, d.name, None, "queued", time.time(), time.time())
        return {"status": "queued", "job": cur.lastrowid}
    if not (d / "fetch.json").exists():
        # failed link stub: nothing was downloaded, so move it aside and fetch the link again from scratch
        dest = TRASH / f"{d.name}_{int(time.time())}"
        shutil.move(str(d), str(dest))
        try:
            return enqueue(r["source_url"])
        except Exception:
            shutil.move(str(dest), str(d))
            raise
    return enqueue(r["source_url"], recipe_id=d.name)


@app.post("/api/recipes/{rid}/thumbs")
def make_thumbs(rid: str):
    """(Re)cut cover candidates from the saved video: stills spread across its length."""
    d = rdir(rid)
    if not (d / "source.mp4").exists():
        raise HTTPException(400, "this recipe has no saved video")
    if not fetch.make_thumbs(d):
        raise HTTPException(500, "could not read frames from the video")
    return load(d)


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
    # no recipe.json yet while a job runs, but the downloaded thumbnail must already be servable
    if not re.fullmatch(r"[\w.\-]+", rid) or not (RECIPES / rid).is_dir():
        raise HTTPException(404)
    d = RECIPES / rid
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
