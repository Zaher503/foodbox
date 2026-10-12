"""Foodbox model benchmark: run every candidate Ollama model through the real pipeline and compare.

    .venv\\Scripts\\python.exe scripts\\bench.py                 # everything in links.txt, all models
    .venv\\Scripts\\python.exe scripts\\bench.py --models qwen3.5:4b gemma4:e2b --limit 2
    .venv\\Scripts\\python.exe scripts\\bench.py --report-only   # rebuild bench\\report.html from saved results

Per link (once, shared by all models so every model sees identical input):
  fetch -> Whisper large-v3 transcript (cached) -> thumbnails.
Per (link, model): the model is unloaded, then app/process.py runs in a FRESH python process
(stateless /api/generate calls, so no context carries over), and the model is unloaded again.
Measured: cold-load time, peak VRAM, GPU/CPU split, tokens/s, per-call time, JSON failures,
frame OCR text, recipe quality (vs JSON-LD / your expected file / the other models' consensus).
Results are saved after every run, so an interrupted benchmark resumes where it stopped.
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
BENCH = ROOT / "bench"
DATA = BENCH / "data" / "recipes"        # fetch.py writes here (FOODBOX_DATA=bench/data)
RUNS = BENCH / "runs"
EXPECTED = BENCH / "expected"            # optional hand-written answers: bench/expected/<link id>.json
RESULTS = BENCH / "results.json"
OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODELS = ["qwen2.5vl:7b", "qwen3.5:4b", "qwen3.5:2b", "gemma4:e4b", "gemma4:e2b", "gemma3:4b"]
RUN_TIMEOUT = 1500
ENV = {**os.environ, "PYTHONUTF8": "1"}

PURPOSES = [("This is one frame", "frame_ocr"), ("You extract a cooking recipe from the text", "struct_page"),
            ("You extract a cooking recipe", "struct_video"), ("Pick the meal", "meal"),
            ("Extract the nutrition", "nutrition"), ("How many servings", "servings"),
            ("Extract the preparation", "times"), ("Suggest 3 to 6", "tags")]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def link_id(s):
    return hashlib.sha1(s.encode()).hexdigest()[:6]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def words(s):
    return len(re.findall(r"\w+", s or ""))


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ Ollama / GPU helpers
def ollama_up():
    try:
        return requests.get(f"{OLLAMA}/api/version", timeout=3).ok
    except Exception:
        return False


def ensure_server():
    if ollama_up():
        return
    exe = shutil.which("ollama") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Ollama/ollama.exe")
    log("starting ollama serve")
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if ollama_up():
            return
        time.sleep(1)
    sys.exit("Ollama server did not start")


def installed_models():
    return {m["name"] for m in requests.get(f"{OLLAMA}/api/tags", timeout=10).json().get("models", [])}


def ps():
    try:
        return requests.get(f"{OLLAMA}/api/ps", timeout=5).json().get("models", [])
    except Exception:
        return []


def unload_all():
    """Evict every loaded model so the next call is a cold start with an empty KV cache."""
    for m in ps():
        try:
            requests.post(f"{OLLAMA}/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=30)
        except Exception:
            pass
    for _ in range(40):
        if not ps():
            return
        time.sleep(0.5)


def vram_used_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        return int(out)
    except Exception:
        return None


def gpu_name():
    try:
        return subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                              capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
    except Exception:
        return "unknown GPU"


class Sampler(threading.Thread):
    """Polls nvidia-smi and /api/ps while a run is in progress."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_ev = threading.Event()
        self.baseline = vram_used_mb() or 0
        self.peak = self.baseline
        self.size = self.size_vram = 0

    def run(self):
        while not self.stop_ev.is_set():
            u = vram_used_mb()
            if u:
                self.peak = max(self.peak, u)
            for m in ps():
                if m.get("size", 0) >= self.size:
                    self.size, self.size_vram = m.get("size", 0), m.get("size_vram", 0)
            self.stop_ev.wait(0.5)

    def finish(self):
        self.stop_ev.set()
        self.join(timeout=5)
        return {"vram_baseline_mb": self.baseline, "vram_peak_mb": self.peak,
                "vram_delta_mb": self.peak - self.baseline,
                "model_mb": round(self.size / 1e6), "model_gpu_mb": round(self.size_vram / 1e6),
                "gpu_pct": round(100 * self.size_vram / self.size) if self.size else None}


# ------------------------------------------------------------------ prep: fetch + whisper + thumbs
def parse_links(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, note = line.partition("|")
        out.append({"src": url.strip().strip('"'), "note": note.strip()})
    return out


def ffprobe_duration(p):
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                     str(p)], capture_output=True, text=True).stdout.strip())
    except Exception:
        return None


def prep_link(link, force=False):
    lid = link_id(link["src"])
    d = next(iter(DATA.glob(f"*_{lid}")), None) if DATA.exists() else None
    if not d or force or not (d / "fetch.json").exists():
        if Path(link["src"]).is_file():                      # local video file
            d = DATA / f"local_{slug(Path(link['src']).stem)[:40]}_{lid}"
            d.mkdir(parents=True, exist_ok=True)
            if not (d / "source.mp4").exists():
                try:
                    os.link(link["src"], d / "source.mp4")
                except OSError:
                    shutil.copy(link["src"], d / "source.mp4")
            write_json(d / "fetch.json", {"kind": "video", "source_url": link["src"], "caption": "",
                                         "yt_title": Path(link["src"]).stem,
                                         "duration": ffprobe_duration(d / "source.mp4")})
        else:
            log(f"fetch {link['src']}")
            p = subprocess.run([sys.executable, str(APP / "fetch.py"), link["src"]], capture_output=True,
                               text=True, env={**ENV, "FOODBOX_DATA": str(BENCH / "data")}, timeout=900)
            d = next(iter(DATA.glob(f"*_{lid}")), None) if DATA.exists() else None
            if not d or not (d / "fetch.json").exists():
                return {"id": lid, **link, "error": ((p.stdout + p.stderr).strip() or "fetch failed")[-400:]}
    fx = read_json(d / "fetch.json", {})
    info = {"id": lid, **link, "dir": str(d), "kind": fx.get("kind"), "title": fx.get("yt_title") or fx.get("page_title"),
            "has_jsonld": bool(fx.get("has_jsonld")), "caption_words": words(fx.get("caption")),
            "duration": fx.get("duration"), "error": None}
    if fx.get("kind") == "video":
        tfile = d / "transcript.cache.txt"
        if force or not tfile.exists():
            log(f"whisper large-v3 on {d.name}")
            s = Sampler(); s.start(); t0 = time.time()
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker-whisper", str(d)],
                               capture_output=True, text=True, env=ENV, timeout=1800)
            wh = {"seconds": round(time.time() - t0, 1), **s.finish()}
            if not tfile.exists():
                info["error"] = "whisper failed: " + (p.stdout + p.stderr).strip()[-400:]
                return info
            write_json(d / "whisper.json", wh)
        info["whisper"] = read_json(d / "whisper.json", {})
        info["transcript_words"] = words(tfile.read_text(encoding="utf-8"))
        info["speech_only"] = info["caption_words"] < 15
        thumbs = d / "thumbs"
        if not thumbs.exists():
            thumbs.mkdir()
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(d / "source.mp4"), "-vf", "fps=1/2,scale=320:-2",
                            "-q:v", "6", "-frames:v", "40", str(thumbs / "t_%03d.jpg")])
        info["modes"] = ["video"]
    else:
        info["modes"] = ["jsonld", "text"] if info["has_jsonld"] else ["page"]
    return info


def worker_whisper(d):
    sys.path.insert(0, str(APP))
    import process
    text = process.transcribe(Path(d) / "source.mp4")
    (Path(d) / "transcript.cache.txt").write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ worker: one (link, model, mode) run
def worker_run(a):
    """Runs in a fresh python process. Patches process.ollama to time/record every call."""
    os.environ["FOODBOX_MODEL"] = a.model
    if a.mode == "text":
        os.environ["FORCE_TEXT"] = "1"
    sys.path.insert(0, str(APP))
    import base64
    import process
    work, src = Path(a.work), Path(a.src)
    calls = []

    def ollama(prompt, images=None, num_ctx=8192, fmt="json", timeout=300):
        purpose = next((p for k, p in PURPOSES if prompt.startswith(k)), "other")
        body = {"model": a.model, "prompt": prompt, "stream": False, "format": fmt,
                "options": {"num_ctx": num_ctx, "temperature": 0.1}}
        if not a.think:
            body["think"] = False
        if images:
            body["images"] = [base64.b64encode(Path(i).read_bytes()).decode() for i in images]
        rec = {"purpose": purpose, "num_ctx": num_ctx, "ok": False}
        t0 = time.time()
        try:
            r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=timeout)
            if r.status_code == 400 and "think" in r.text:      # model has no thinking switch
                body.pop("think")
                r = requests.post(f"{OLLAMA}/api/generate", json=body, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            rec.update(wall_s=round(time.time() - t0, 2), load_s=round(j.get("load_duration", 0) / 1e9, 2),
                       prompt_tokens=j.get("prompt_eval_count"), out_tokens=j.get("eval_count"),
                       eval_s=round(j.get("eval_duration", 0) / 1e9, 2),
                       tps=round(j["eval_count"] / (j["eval_duration"] / 1e9), 1)
                       if j.get("eval_count") and j.get("eval_duration") else None,
                       thinking_chars=len(j.get("thinking") or ""), resp_chars=len(j.get("response") or ""))
            if not (j.get("response") or "").strip():
                rec["error"] = "empty response"
                raise ValueError("empty response")
            try:
                out = json.loads(j["response"])
            except json.JSONDecodeError:
                rec["error"] = "invalid json"
                raise
            rec["ok"] = True
            return out
        except Exception as e:
            rec.setdefault("wall_s", round(time.time() - t0, 2))
            rec.setdefault("error", repr(e)[:150])
            raise
        finally:
            calls.append(rec)

    process.ollama = ollama
    process.transcribe = lambda video: (src / "transcript.cache.txt").read_text(encoding="utf-8")
    t0 = time.time()
    out = {"ok": False, "error": None}
    try:
        r, lg = process.process(work)
        out.update(ok=True, recipe=r, log=lg)
    except Exception as e:
        out["error"] = repr(e)[:300]
    out["wall_s"] = round(time.time() - t0, 1)
    out["calls"] = calls
    # evidence the model was given, for grounding checks later
    fx = read_json(work / "fetch.json", {})
    parts = [fx.get("caption", "")]
    for name in ("transcript.txt", "page.txt"):
        if (work / name).exists():
            parts.append((work / name).read_text(encoding="utf-8"))
    ocr = read_json(work / "ocr.json", [])
    parts.append(process.dedupe_text(s.get("text", "") for s in ocr))
    (work / "corpus.txt").write_text("\n".join(parts), encoding="utf-8")
    write_json(a.out, out)


# ------------------------------------------------------------------ orchestration
def stage_workdir(info, model, mode, rep):
    src = Path(info["dir"])
    work = RUNS / info["id"] / f"{slug(model)}__{mode}__{rep}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for f in src.iterdir():
        if f.name in ("thumbs", "transcript.cache.txt", "whisper.json") or f.is_dir():
            continue
        if f.suffix == ".mp4":
            try:
                os.link(f, work / f.name)
            except OSError:
                shutil.copy(f, work / f.name)
        else:
            shutil.copy(f, work / f.name)
    return src, work


def load_probe(model):
    """Cold-load the model once with the pipeline's default context and record load time + VRAM split."""
    unload_all()
    s = Sampler(); s.start()
    t0 = time.time()
    rec = {"ok": False}
    try:
        r = requests.post(f"{OLLAMA}/api/generate", json={
            "model": model, "prompt": "Reply with the single word OK.", "stream": False, "think": False,
            "options": {"num_ctx": 8192, "temperature": 0}}, timeout=600)
        if r.status_code == 400:
            r = requests.post(f"{OLLAMA}/api/generate", json={
                "model": model, "prompt": "Reply with the single word OK.", "stream": False,
                "options": {"num_ctx": 8192, "temperature": 0}}, timeout=600)
        r.raise_for_status()
        j = r.json()
        rec.update(ok=True, wall_s=round(time.time() - t0, 1), load_s=round(j.get("load_duration", 0) / 1e9, 1))
        time.sleep(1.2)
    except Exception as e:
        rec["error"] = repr(e)[:200]
    rec.update(s.finish())
    unload_all()
    return rec


def run_one(info, model, mode, rep, think):
    src, work = stage_workdir(info, model, mode, rep)
    out_path = work / "_result.json"
    unload_all()
    s = Sampler(); s.start()
    t0 = time.time()
    p = None
    try:
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker-run", "--src", str(src),
                            "--work", str(work), "--model", model, "--mode", mode, "--out", str(out_path)]
                           + (["--think"] if think else []),
                           capture_output=True, text=True, env=ENV, timeout=RUN_TIMEOUT)
        (work / "_worker.log").write_text(p.stdout + "\n" + p.stderr, encoding="utf-8")
    except subprocess.TimeoutExpired:
        write_json(out_path, {"ok": False, "error": f"timeout after {RUN_TIMEOUT}s", "calls": []})
    res = read_json(out_path, {"ok": False, "error": "worker crashed: " + (p.stderr.strip()[-300:] if p else ""),
                               "calls": []})
    res.update(s.finish())
    res["elapsed_s"] = round(time.time() - t0, 1)
    unload_all()
    return res


def orchestrate(a):
    BENCH.mkdir(exist_ok=True)
    links_file = Path(a.links)
    if not links_file.exists():
        sys.exit(f"{links_file} not found - put one recipe link (or a local .mp4 path) per line.")
    links = parse_links(links_file)
    if a.limit:
        links = links[:a.limit]
    if not links:
        sys.exit(f"{links_file} has no links")
    ensure_server()
    have = installed_models()
    models = [m for m in a.models if m in have or f"{m}:latest" in have]
    for m in a.models:
        if m not in models:
            log(f"SKIP {m}: not installed (ollama pull {m})")
    if not models:
        sys.exit("no candidate models installed")

    res = read_json(RESULTS, {"runs": {}, "load": {}, "links": {}})
    res["meta"] = {"gpu": gpu_name(), "started": time.strftime("%Y-%m-%d %H:%M"), "models": models,
                   "think": a.think, "repeats": a.repeats,
                   "ollama": requests.get(f"{OLLAMA}/api/version", timeout=5).json().get("version")}

    for link in links:
        info = prep_link(link, a.force)
        res["links"][info["id"]] = info
        if info.get("error"):
            log(f"LINK FAILED {link['src']}: {info['error']}")
        write_json(RESULTS, res)

    todo = []
    for model in models:
        for link in links:
            info = res["links"][link_id(link["src"])]
            if info.get("error"):
                continue
            for mode in info["modes"]:
                for rep in range(a.repeats):
                    todo.append((model, info, mode, rep))
    log(f"{len(models)} models x {len(links)} links -> {len(todo)} runs (+{len(models)} load probes)")

    for model in models:
        if model not in res["load"] or a.force:
            log(f"load probe {model}")
            res["load"][model] = load_probe(model)
            write_json(RESULTS, res)
        for (m, info, mode, rep) in [t for t in todo if t[0] == model]:
            key = f"{info['id']}|{mode}|{model}|{rep}"
            if key in res["runs"] and not a.force and res["runs"][key].get("ok"):
                continue
            log(f"RUN {model} | {mode} | {info['title'] or info['src']}"[:150])
            r = run_one(info, model, mode, rep, a.think)
            r.update(link=info["id"], model=model, mode=mode, rep=rep)
            res["runs"][key] = r
            write_json(RESULTS, res)
            log(f"   -> {'ok' if r.get('ok') else 'FAIL ' + str(r.get('error'))} in {r['elapsed_s']}s, "
                f"peak +{r['vram_delta_mb']} MB, gpu {r['gpu_pct']}%")
    unload_all()
    build_report()


# ------------------------------------------------------------------ scoring + report
UNITS = set("cup cups c tbsp tablespoon tablespoons tsp teaspoon teaspoons oz ounce ounces lb lbs pound pounds g gram "
            "grams kg ml l liter pinch dash clove cloves can cans slice slices stick sticks large medium small whole "
            "package packages bunch handful to taste of and or a the for fresh freshly chopped diced minced sliced "
            "grated melted softened divided optional about plus more cut into inch pieces room temperature finely "
            "thinly roughly cooked uncooked extra plain your favorite such as preferably needed total".split())


def ing_tokens(s):
    s = re.split(r",| - ", str(s).lower())[0]
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[\d½¼¾⅓⅔/.\-]+", " ", s)
    toks = set()
    for t in re.findall(r"[a-z]{3,}", s):
        if t in UNITS:
            continue
        toks.add(t[:-1] if t.endswith("s") and len(t) > 3 else t)
    return toks


def same_item(a, b):
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= 0.67


def words_of(text):
    return {(t[:-1] if t.endswith("s") and len(t) > 3 else t) for t in re.findall(r"[a-z]{3,}", text.lower())}


def consensus_ref(lists):
    """Items that at least half of the given ingredient lists agree on."""
    lists = [[ing_tokens(i) for i in l] for l in lists if l]
    if len(lists) < 2:
        return None
    need = math.ceil(len(lists) / 2)
    ref = []
    for l in lists:
        for t in l:
            if t and not any(same_item(t, r) for r in ref):
                if sum(any(same_item(t, x) for x in other) for other in lists) >= need:
                    ref.append(t)
    return ref or None


def run_metrics(run, ref, ref_kind, corpus_words, ref_title):
    r = run.get("recipe") or {}
    ings, steps = r.get("ingredients") or [], r.get("steps") or []
    toks = [ing_tokens(i) for i in ings]
    m = {"n_ing": len(ings), "n_steps": len(steps), "empty": not ings,
         "qty_cov": round(sum(bool(re.search(r"\d|½|¼|¾|⅓|⅔", i)) for i in ings) / len(ings), 2) if ings else None,
         "avg_step_words": round(sum(words(s) for s in steps) / len(steps), 1) if steps else None,
         "dropped": (run.get("log") or {}).get("dropped"), "needs_review": r.get("needs_review"),
         "has_servings": bool(r.get("servings_num")), "servings_inferred": r.get("servings_inferred"),
         "has_times": any(r.get(k) for k in ("prep_min", "cook_min", "total_min")),
         "has_nutrition": bool(r.get("nutrition")), "n_tags": len(r.get("tags") or []), "meal": r.get("meal")}
    flags = []
    for i, t in zip(ings, toks):
        flags.append({"t": i, "ref": any(same_item(t, x) for x in ref) if ref else None,
                      "grounded": (len(t & corpus_words) / len(t) >= 0.6) if (t and corpus_words) else None})
    m["ing_flags"] = flags
    if corpus_words and ings:
        g = [f["grounded"] for f in flags if f["grounded"] is not None]
        m["grounded"] = round(sum(g) / len(g), 2) if g else None
    if ref and ings:
        m["recall"] = round(sum(any(same_item(x, t) for t in toks) for x in ref) / len(ref), 2)
        m["precision"] = round(sum(f["ref"] for f in flags) / len(flags), 2)
        m["missed"] = [" ".join(sorted(x)) for x in ref if not any(same_item(x, t) for t in toks)][:12]
    elif ref:
        m["recall"], m["precision"], m["missed"] = 0.0, None, [" ".join(sorted(x)) for x in ref][:12]
    m["ref_kind"] = ref_kind
    if ref_title and r.get("title"):
        a, b = words_of(ref_title), words_of(r["title"])
        m["title_match"] = round(len(a & b) / max(1, len(a | b)), 2)
    calls = run.get("calls") or []
    m["n_calls"], m["n_fail"] = len(calls), sum(not c.get("ok") for c in calls)
    m["bad_json"] = sum(c.get("error") == "invalid json" for c in calls)
    m["empty_resp"] = sum(c.get("error") == "empty response" for c in calls)
    by = {}
    for c in calls:
        b = by.setdefault(c["purpose"], {"n": 0, "s": 0.0, "fail": 0, "tok": 0, "think": 0})
        b["n"] += 1
        b["s"] = round(b["s"] + (c.get("wall_s") or 0), 2)
        b["fail"] += not c.get("ok")
        b["tok"] += c.get("out_tokens") or 0
        b["think"] += c.get("thinking_chars") or 0
    m["by_purpose"] = by
    m["load_s"] = round(sum(c.get("load_s") or 0 for c in calls), 1)
    m["n_loads"] = sum(1 for c in calls if (c.get("load_s") or 0) > 0.5)
    tps = [c["tps"] for c in calls if c.get("tps") and c["purpose"] != "frame_ocr"] or [c["tps"] for c in calls if c.get("tps")]
    m["tps"] = round(sum(tps) / len(tps), 1) if tps else None
    m["frame_s"] = round(by.get("frame_ocr", {}).get("s", 0) / by["frame_ocr"]["n"], 2) if by.get("frame_ocr", {}).get("n") else None
    return m


def build_report():
    res = read_json(RESULTS)
    if not res:
        sys.exit("no results yet")
    runs = res["runs"]
    groups = {}
    for k, r in runs.items():
        groups.setdefault((r["link"], r["mode"], r["rep"]), []).append(r)
    out_runs = []
    for (lid, mode, rep), grp in groups.items():
        info = res["links"].get(lid, {})
        ld = Path(info.get("dir", ""))
        exp = read_json(EXPECTED / f"{lid}.json")
        jl = read_json(ld / "jsonld.json") if mode == "text" else None
        ref_title = None
        for r in grp:
            wd = RUNS / lid / f"{slug(r['model'])}__{mode}__{rep}"
            cp = wd / "corpus.txt"
            corpus_words = words_of(cp.read_text(encoding="utf-8")) if cp.exists() and mode != "jsonld" else None
            if exp and exp.get("ingredients"):
                ref, kind, ref_title = [ing_tokens(i) for i in exp["ingredients"]], "expected", exp.get("title")
            elif jl and jl.get("recipeIngredient"):
                ref, kind, ref_title = [ing_tokens(i) for i in jl["recipeIngredient"]], "jsonld", jl.get("name")
            elif mode == "jsonld":
                ref, kind = None, None
            else:
                others = [(o.get("recipe") or {}).get("ingredients") for o in grp if o is not r]
                ref, kind = consensus_ref(others), "consensus"
            m = run_metrics(r, ref, kind if ref else None, corpus_words, ref_title)
            row = {"key": f"{lid}|{mode}|{r['model']}|{rep}", "link": lid, "mode": mode, "model": r["model"], "rep": rep,
                   "ok": r.get("ok"), "error": r.get("error"), "elapsed_s": r.get("elapsed_s"),
                   "vram_delta_mb": r.get("vram_delta_mb"), "vram_peak_mb": r.get("vram_peak_mb"),
                   "gpu_pct": r.get("gpu_pct"), "model_mb": r.get("model_mb"), "m": m,
                   "recipe": {k: v for k, v in (r.get("recipe") or {}).items() if k not in ("cover", "has_video")}}
            ocr = read_json(wd / "ocr.json", [])
            row["ocr"] = [{"f": o.get("frame"), "t": o.get("text", ""), "err": o.get("err")} for o in ocr]
            out_runs.append(row)
    links = {}
    for lid, info in res["links"].items():
        ld = Path(info.get("dir", ""))
        d = {k: v for k, v in info.items() if k != "dir"}
        if info.get("error"):
            links[lid] = d
            continue
        fx = read_json(ld / "fetch.json", {})
        d["caption"] = (fx.get("caption") or "")[:1500]
        tf = ld / "transcript.cache.txt"
        d["transcript"] = tf.read_text(encoding="utf-8")[:3000] if tf.exists() else ""
        d["thumbs"] = []
        import base64
        for t in sorted((ld / "thumbs").glob("*.jpg")) if (ld / "thumbs").exists() else []:
            d["thumbs"].append("data:image/jpeg;base64," + base64.b64encode(t.read_bytes()).decode())
        links[lid] = d
    data = {"meta": res.get("meta", {}), "load": res.get("load", {}), "links": links, "runs": out_runs,
            "generated": time.strftime("%Y-%m-%d %H:%M")}
    tpl = (Path(__file__).parent / "bench_report.html").read_text(encoding="utf-8")
    html = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (BENCH / "report.html").write_text(html, encoding="utf-8")
    log(f"report written: {BENCH / 'report.html'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--links", default=str(ROOT / "links.txt"))
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--limit", type=int, help="only the first N links (smoke test)")
    ap.add_argument("--repeats", type=int, default=1, help="repeat each run N times to see run-to-run variance")
    ap.add_argument("--think", action="store_true", help="leave model 'thinking' on (default: off, like a fast extractor)")
    ap.add_argument("--force", action="store_true", help="redo fetch/whisper/runs even if cached")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--worker-whisper")
    ap.add_argument("--worker-run", action="store_true")
    for n in ("src", "work", "model", "mode", "out"):
        ap.add_argument(f"--{n}")
    a = ap.parse_args()
    if a.worker_whisper:
        return worker_whisper(a.worker_whisper)
    if a.worker_run:
        return worker_run(a)
    if a.report_only:
        return build_report()
    orchestrate(a)


if __name__ == "__main__":
    main()
