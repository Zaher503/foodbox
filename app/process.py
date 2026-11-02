"""Step 3: turn a fetched folder into recipe.json. Whisper -> frame OCR -> structure. Cover is always thumbnail.jpg."""
import base64
import ctypes
import glob
import hashlib
import html as htmllib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import requests

from tags import make_tags

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/generate"
VL = os.environ.get("FOODBOX_MODEL") or os.environ.get("RECIPE_VL") or "gemma4:e4b"  # main model; env overrides for A/B tests
MEALS = ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Other"]
# Shared AI hub (queue + Whisper). Unset means this app runs its own Whisper and talks to a plain Ollama.
WHISPER_URL = os.environ.get("WHISPER_URL", "").rstrip("/")
WHISPER_MODEL = os.environ.get("FOODBOX_WHISPER_MODEL", "large-v3")
QUEUE_PATIENCE = float(os.environ.get("FOODBOX_QUEUE_PATIENCE", "1800"))  # extra seconds a call may wait in the hub's line
_job: ContextVar = ContextVar("foodbox_job", default=None)  # [recipe folder, current stage key] of the job being processed


def _hub_headers(audio_seconds=None) -> dict:
    h = {"X-Client": "foodbox", "X-Priority": "normal", "X-Queue-Ticket": uuid.uuid4().hex} if WHISPER_URL else {}
    if audio_seconds:
        h["X-Audio-Seconds"] = str(round(audio_seconds, 1))
    return h


def _announce_wait(kind, model, audio_seconds=None):
    """Ask the hub how long the line is; when it is long, show "Waiting for the AI queue" instead of looking stuck."""
    cur = _job.get()
    if not WHISPER_URL or cur is None:
        return
    try:
        params = {"kind": kind, "model": model, "priority": "normal"}
        if audio_seconds:
            params["audio_seconds"] = round(audio_seconds, 1)
        est = requests.get(f"{WHISPER_URL}/queue/estimate", params=params, timeout=3).json()
    except Exception:
        return
    if est.get("wait_seconds", 0) >= 5:
        stage(cur[0], "waiting", note=f"{est.get('position', 0)} ahead, about {round(est['wait_seconds'])} s")


def _after_wait():
    cur = _job.get()
    if cur is not None and WHISPER_URL:
        stage(cur[0], cur[1])


def _preload_cuda():
    """Make the pip-installed CUDA libs (nvidia-cublas/cudnn) findable by CTranslate2."""
    import nvidia.cublas, nvidia.cudnn
    for pkg in (nvidia.cublas, nvidia.cudnn):
        root = list(pkg.__path__)[0]
        if os.name == "nt":
            d = os.path.join(root, "bin")
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            continue
        for so in sorted(glob.glob(os.path.join(root, "lib", "*.so*"))):
            try:
                ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def _whisper():
    """Whisper on the GPU when one is visible, otherwise a smaller int8 model on the CPU."""
    dev = os.environ.get("FOODBOX_WHISPER_DEVICE", "auto")
    if dev == "auto":
        try:
            import ctranslate2
            dev = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            dev = "cpu"
    from faster_whisper import WhisperModel
    if dev == "cuda":
        _preload_cuda()
        return WhisperModel(os.environ.get("FOODBOX_WHISPER_MODEL", "large-v3"), device="cuda", compute_type="int8_float16")
    return WhisperModel(os.environ.get("FOODBOX_WHISPER_MODEL", "small"), device="cpu", compute_type="int8")


JUNK = re.compile(r"thank(s| you) (so much )?for (watching|listening)|subscribe|^\W*$", re.I)


def transcribe_hub(video: Path) -> str:
    """Whisper in the shared AI hub: send the audio, get the segments back."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
        seconds = max((wav.stat().st_size - 44) / 32000, 0.0)
        if seconds < 1:
            return ""
        _announce_wait("transcribe", WHISPER_MODEL, seconds)
        try:
            with open(wav, "rb") as f:
                r = requests.post(f"{WHISPER_URL}/v1/audio/transcriptions", files={"file": ("audio.wav", f, "audio/wav")},
                                  data={"model": WHISPER_MODEL, "vad_filter": "true", "beam_size": "5"},
                                  headers={**_hub_headers(seconds), "X-Model": WHISPER_MODEL}, timeout=3600 + QUEUE_PATIENCE)
            r.raise_for_status()
        finally:
            _after_wait()
    segs = r.json().get("segments", [])
    return " ".join(g["text"].strip() for g in segs
                    if g.get("no_speech_prob", 0) < 0.5 and g.get("avg_logprob", 0) > -1.0 and not JUNK.search(g.get("text", "")))


def transcribe(video: Path) -> str:
    if WHISPER_URL:
        return transcribe_hub(video)
    m = _whisper()
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    segs, _ = m.transcribe(audio, vad_filter=True, beam_size=5, condition_on_previous_text=False)
    junk = re.compile(r"thank(s| you) (so much )?for (watching|listening)|subscribe|^\W*$", re.I)
    text = " ".join(s.text.strip() for s in segs
                    if s.no_speech_prob < 0.5 and s.avg_logprob > -1.0 and not junk.search(s.text))
    del m
    return text


def extract_frames(video: Path, out: Path, every=2.0, width=768):
    out.mkdir(exist_ok=True)
    for f in out.glob("f_*.jpg"):
        f.unlink()
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf",
                    f"fps=1/{every},scale={width}:-2", "-q:v", "3",
                    str(out / "f_%03d.jpg")], check=True)
    return sorted(out.glob("f_*.jpg"))


def ollama(prompt, images=None, num_ctx=8192, fmt="json", timeout=300):
    if WHISPER_URL:
        timeout += QUEUE_PATIENCE  # the call may first wait its turn in the hub
        _announce_wait("generate", VL)
    try:
        return _ollama(prompt, images, num_ctx, fmt, timeout)
    finally:
        _after_wait()


def _ollama(prompt, images, num_ctx, fmt, timeout):
    body = {"model": VL, "prompt": prompt, "stream": False, "format": fmt,
            "options": {"num_ctx": num_ctx, "temperature": 0.1}, "think": False}  # no reasoning tokens: faster, JSON stays clean
    if images:
        body["images"] = [base64.b64encode(Path(i).read_bytes()).decode() for i in images]
    r = requests.post(OLLAMA, json=body, headers=_hub_headers(), timeout=timeout)
    if r.status_code == 400 and "think" in r.text:  # model has no thinking switch
        body.pop("think")
        r = requests.post(OLLAMA, json=body, headers=_hub_headers(), timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["response"])


FRAME_PROMPT = (
    "This is one frame from a cooking video. Return JSON with keys: "
    '"text": all readable on-screen text copied exactly (ingredient names and quantities, step text), '
    "empty string if none."
)


def scan_frames(frames):
    out = []
    for f in frames:
        try:
            j = ollama(FRAME_PROMPT, [f], num_ctx=4096, timeout=120)
            out.append({"frame": f.name, "text": str(j.get("text", "")).strip()})
        except Exception as e:
            out.append({"frame": f.name, "text": "", "err": str(e)[:80]})
    return out


def dedupe_text(items):
    seen, out = set(), []
    for t in items:
        key = re.sub(r"\W+", " ", t.lower()).strip()
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return "\n".join(out)


STRUCT_PROMPT = """You extract a cooking recipe. Use ONLY the sources below. Do not invent quantities or steps.
Output JSON: {{"title": str, "meal": one of {meals}, "servings": str or null,
"ingredients": [str, ...], "steps": [str, ...]}}
Rules:
- No intro, no backstory, no tips, no hashtags, no promotion. Just the recipe.
- NEVER guess. Every number, quantity, time and temperature must appear in the sources below. If a quantity is not stated, write the ingredient name alone with no amount. Do not add ingredients or steps that are not stated or clearly named in the sources.
- If the sources contain no recipe details at all, return empty "ingredients" and "steps" lists.
- Ignore brand names and text printed on product packaging.
- Ingredients: "quantity unit item, preparation" exactly as given; if no quantity is given write the item alone.
- Keep the preparation of every ingredient: size/cut and state ("1.5 lbs boneless skinless chicken thighs, cut into 1 inch pieces", "5 cloves garlic, grated", "1 onion, diced"). Take it from any source (caption list, spoken words, on-screen text).
- If the CAPTION contains an ingredient list with amounts, use it as the base: keep every line with its amount and prep note, and fill in a missing ingredient name from the transcript (a line like "1.5 lbs boneless, cut into 1 in pieces" plus the spoken "chicken thighs" becomes "1.5 lbs boneless chicken thighs, cut into 1 inch pieces").
- Steps: short imperative sentences in cooking order, include temperatures and times when stated.
- Never write a step that only names a stage ("Cook the chicken:"). When the method has parts (chicken, sauce, rice), put a heading line before that part's steps, written as "## For the sauce". Use headings only when there are two or more parts; a single-part recipe has none.
- Title: the dish name only.
- The transcript is speech-to-text and can mishear cooking words ("cating" for "coating"); fix obvious mishearings using context.
- Sources may disagree; prefer on-screen text for quantities, spoken transcript for method.

CAPTION:
{caption}

SPOKEN TRANSCRIPT:
{transcript}

ON-SCREEN TEXT (deduplicated):
{ocr}
"""

from amounts import fill_caption_amounts

QTY_START = re.compile(r"^(\d|[¼-¾⅐-⅞]|a pinch|a dash|a handful)", re.I)


def caption_ingredients(caption):
    """Ingredient lines written in a caption ("- 1 cup flour - 2 eggs" or one per line). [] if there is no list."""
    parts = re.split(r"(?:^|\s)[-•▪*]\s+", caption or "")[1:]
    items = [re.sub(r"\s+", " ", p).strip(" ,;") for p in parts]
    items = [i for i in items if i and not i.startswith("#")]
    good = [i for i in items if QTY_START.match(i)]
    return good if len(good) >= 3 and len(good) >= len(items) * 0.7 else []


def keep_caption_amounts(r, cap_items):
    """If the model dropped the amounts the caption lists, use the caption's lines as they are."""
    have = sum(1 for i in (r.get("ingredients") or []) if re.search(r"\d", i))
    if cap_items and have < len(cap_items) * 0.7:
        r["ingredients"] = list(cap_items)
    return r


HEAD_LIKE = re.compile(r"^[A-Z][^.!?]{0,60}:$")


def norm_steps(steps):
    """Section titles become "## Title" entries (a bare "Cook the chicken:" line is a title, not a step)."""
    out = []
    for s in steps or []:
        s = str(s).strip()
        m = re.match(r"^#{1,6}\s*(.+?)\s*:?$", s)
        if m:
            s = "## " + m.group(1)
        elif HEAD_LIKE.match(s) and len(s.split()) <= 8:
            s = "## " + s.rstrip(":").strip()
        if s.startswith("## ") and out and out[-1].startswith("## "):
            out.pop()  # a title with nothing under it
        out.append(s)
    while out and out[-1].startswith("## "):
        out.pop()
    if sum(1 for s in out if s.startswith("## ")) == 1:
        out = [s for s in out if not s.startswith("## ")]  # one title alone splits nothing
    return out


def norm_ingredients(items):
    """Ingredient group titles ("## For the chicken") stay only when two or more groups have items under them."""
    out = []
    for s in items or []:
        s = str(s).strip()
        m = re.match(r"^#{1,6}\s*(.+?)\s*:?$", s)
        if m:
            s = "## " + m.group(1)
        if s.startswith("## ") and out and out[-1].startswith("## "):
            out.pop()
        out.append(s)
    while out and out[-1].startswith("## "):
        out.pop()
    if sum(1 for s in out if s.startswith("## ")) == 1:
        out = [s for s in out if not s.startswith("## ")]  # one title alone splits nothing
    return out


def html_ingredient_groups(html: str):
    """[(title, n_items), ...] from recipe-card plugins that group ingredients; schema.org data is flat."""
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    cls = lambda n: f"contains(concat(' ', normalize-space(@class), ' '), ' {n} ')"
    groups = []
    for g in doc.xpath(f"//*[{cls('wprm-recipe-ingredient-group')}]"):
        names = g.xpath(f".//*[{cls('wprm-recipe-group-name')}]")
        name = names[0].text_content() if names else ""
        items = g.xpath(f".//li[{cls('wprm-recipe-ingredient')}]")
        if items:
            groups.append((re.sub(r"\s+", " ", unescape(name)).strip().rstrip(":"), len(items)))
    return groups


def apply_groups(ingredients, groups):
    """Put the page's group titles into the flat schema.org list when the counts line up."""
    if len(groups) < 2 or sum(n for _, n in groups) != len(ingredients):
        return ingredients
    out, i = [], 0
    for title, n in groups:
        if title:
            out.append("## " + title)
        out += ingredients[i:i + n]
        i += n
    return out


def unescape(text):
    """&#39; -> ' (twice, for pages that escape the markup they already escaped)."""
    text = str(text)
    for _ in range(2):
        if "&" not in text:
            break
        text = htmllib.unescape(text)
    return re.sub("[\u200b\u200c\u200d\u2060\ufeff]", "", text.replace("\xa0", " "))


def tidy_parens(s):
    """Recipe plugins leave "(, minced)" and "((see notes))" behind when a note or comma is empty."""
    s = re.sub(r"\(\s*[,;]\s*", "(", s)
    s = re.sub(r"\(\s*\(([^()]*)\)\s*\)", r"(\1)", s)
    s = re.sub(r"\s+([,;])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    return re.sub(r"\s+\)", ")", s)


def clean_list(x):
    out = []
    for i in (x or []):
        s = re.sub(r"\(\s*\(?\s*(I|we|my|our)\b[^()]*\)?\s*\)", "", unescape(i), flags=re.I)  # "(I like to use ...)"
        s = tidy_parens(re.sub(r"\s+", " ", s).strip())
        if s:
            out.append(s)
    return out


FRAC = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3"}
NUMW = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9", "ten": "10", "half": "1/2", "quarter": "1/4", "dozen": "12"}


def _nums(text):
    text = text.lower()
    for k, v in FRAC.items():
        text = text.replace(k, v)
    found = set(re.findall(r"\d+(?:[./]\d+)?", text))
    found |= {NUMW[w] for w in re.findall(r"[a-z]+", text) if w in NUMW}
    return found


def ground(ingredients, steps, corpus):
    """Strip any number that is not present in the sources. Returns (ing, steps, n_dropped)."""
    have, dropped, changes = _nums(corpus), 0, []

    def fix(line, kind, idx):
        nonlocal dropped
        bad = _nums(line) - have
        if not bad:
            return line
        dropped += 1
        was = line
        for n in bad:
            line = re.sub(rf"(?<![\d./]){re.escape(n)}(?![\d./])\s*(cups?|tbsp|tsp|tablespoons?|teaspoons?|lbs?|pounds?|oz|ounces?|g|grams?|kg|ml|l|minutes?|mins?|hours?|degrees?|°[CF]?)?\b\.?\s*(of )?", "", line, flags=re.I)
        line = re.sub(r"\s+", " ", line).strip(" ,;")
        changes.append({"kind": kind, "index": idx, "was": was})  # remembered so the UI can point at the line to check
        return line

    ing = [fix(x, "ingredient", i) for i, x in enumerate(ingredients)]
    stp = [fix(x, "step", i) for i, x in enumerate(steps)]
    return ing, stp, dropped, changes


ACTIVE_TIME = re.compile(r"\bActive:\s*(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.I)


def active_minutes(html: str):
    """Hands-on time a page labels "Active: 35 min" (Food Network). Its schema.org cookTime is that same figure,
    not the time in the oven, so the caller moves it out of the Cook slot."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    for bad in doc.xpath("//script|//style"):
        bad.getparent().remove(bad)
    for m in ACTIVE_TIME.finditer(re.sub(r"\s+", " ", doc.text_content())):
        n = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
        if 1 <= n <= 2880 and (m.group(1) or m.group(2)):
            return n
    return None


EQUIPMENT = re.compile(r"\s*equipment\s*:", re.I)
STEP_LABEL = re.compile(r"[A-Z][^.!?:]{1,60}:")
STEP_LISTS = ("tasty-recipes-instructions", "wprm-recipe-instructions")


def label_steps(steps, html: str):
    """Recipe cards show a bold label before each step ("Mix sauce ingredients:") that schema.org drops, and may add
    tips as extra list items. Returns (steps with their labels put back, tips that belong to no step)."""
    import lxml.html
    doc = lxml.html.fromstring(html)
    cls = lambda n: f"contains(concat(' ', normalize-space(@class), ' '), ' {n} ')"
    for name in STEP_LISTS:
        # only list items with no list inside, so a tip nested under a step is its own item
        items = doc.xpath(f"//*[{cls(name)}]//li[not(.//li)]")
        if items:
            break
    else:
        return steps, []
    page = [re.sub(r"\s+", " ", unescape(i.text_content())).strip() for i in items]
    out, used = [], set()
    for s in steps:
        new = s
        for n, p in enumerate(page):
            if n not in used and p != s and p.endswith(s) and STEP_LABEL.fullmatch(p[:-len(s)].strip()):
                new = p
                used.add(n)
                break
        else:
            used.update(n for n, p in enumerate(page) if p == s)
        out.append(new)
    extras = [p for n, p in enumerate(page) if n not in used and p and not any(p in o for o in out)]
    return out, extras[:6]


def from_jsonld(d: Path):
    ld = json.loads((d / "jsonld.json").read_text(encoding="utf-8"))
    steps = []
    for s in ld.get("recipeInstructions", []):
        if isinstance(s, str):
            steps.append(s)
        elif s.get("@type") == "HowToSection":
            steps += [x.get("text", "") for x in s.get("itemListElement", [])]
        else:
            steps.append(s.get("text", ""))
    y = ld.get("recipeYield")
    y = y[0] if isinstance(y, list) and y else y
    ingredients = clean_list(ld.get("recipeIngredient"))
    steps = clean_list(re.sub(r"<[^>]+>", "", s) for s in steps)
    extras = []
    try:
        html = (d / "page.html").read_text(encoding="utf-8")
        ingredients = apply_groups(ingredients, html_ingredient_groups(html))
        steps, extras = label_steps(steps, html)
    except Exception:
        pass  # grouping and labels are a bonus; the flat lists are still right
    active = None
    try:
        active = active_minutes(html)
    except Exception:
        pass
    equipment = [i for i in ingredients if EQUIPMENT.match(i)]  # a tool list some recipe cards put among the ingredients
    ingredients = [i for i in ingredients if i not in equipment]
    return {"title": unescape(ld.get("name") or ""), "servings": str(y) if y else None,
            "ingredients": ingredients, "page_extras": [e[:1].upper() + e[1:] for e in equipment] + extras,
            "steps": steps,
            "nutrition": nutrition_from_ld(ld),
            "prep_min": iso_minutes(ld.get("prepTime")), "cook_min": iso_minutes(ld.get("cookTime")),
            "total_min": iso_minutes(ld.get("totalTime")), "active_min": active,
            "category_hint": str(ld.get("recipeCategory", ""))}


def guess_meal(title, ingredients, hint=""):
    j = ollama(f"Pick the meal type for this recipe. Options: {MEALS}. Title: {title}. "
               f"Category hint: {hint}. Ingredients: {ingredients[:12]}. "
               'Return JSON {"meal": "<option>"}', num_ctx=2048, timeout=120)
    return j.get("meal") if j.get("meal") in MEALS else "Other"


PAGE_PROMPT = """You extract a cooking recipe from the text of a web page. Use ONLY the page text below.
Output JSON: {{"title": str, "meal": one of {meals}, "servings": str or null,
"ingredients": [str, ...], "steps": [str, ...]}}
Rules:
- Skip the author's story, introduction, tips, notes, nutrition facts, reviews, ads and links to other recipes. Just the recipe.
- NEVER guess. Copy quantities, times and temperatures exactly as written. Do not add ingredients or steps.
- If the page has no recipe, return empty "ingredients" and "steps" lists.
- Ingredients: "quantity unit item, preparation", one per entry, in the order listed. If the ingredients are split into parts with their own titles ("For the chicken", "Cheese sauce"), put a title entry written "## For the chicken" before that part's ingredients. No titles when there is only one part.  Keep the preparation exactly as written ("diced", "cut into 1 inch pieces", "boneless skinless"). Drop other optional commentary in parentheses.
- Steps: the method in order, one action per entry, short imperative sentences. Keep times and temperatures.
- If the method is split into parts with their own titles ("For the sauce:"), keep each title as its own entry written "## For the sauce" before that part's steps. Never put a title in a step. No headings when the method is one part.
- Title: the recipe's own name exactly as the page titles it (remove site names and the word 'recipe'). Never rename or reinterpret the dish.
- Servings: only if the page states a serving count or yield, otherwise null.

PAGE TITLE: {title}

PAGE TEXT:
{text}
"""

PASTE_PROMPT = """You extract a cooking recipe from pasted text. Use ONLY the text below.
Output JSON: {{"title": str, "meal": one of {meals}, "servings": str or null,
"ingredients": [str, ...], "steps": [str, ...]}}
Rules:
- Skip any intro, backstory, tips, ads or unrelated text that came along with the paste. Just the recipe.
- NEVER guess. Copy quantities, times and temperatures exactly as written. Do not add ingredients or steps that are not stated.
- If the text has no recognizable recipe, return empty "ingredients" and "steps" lists.
- Ingredients: "quantity unit item, preparation", one per entry, in the order listed. If the ingredients are split into parts with their own titles ("For the chicken", "Cheese sauce"), put a title entry written "## For the chicken" before that part's ingredients. No titles when there is only one part.  Keep the preparation exactly as written ("diced", "cut into 1 inch pieces", "boneless skinless"). If a line has no quantity, write the item alone.
- Steps: the method in order, one action per entry, short imperative sentences. Keep times and temperatures. If the ingredients and steps are mixed together or out of order, sort them out; do not reword beyond that.
- If the method is split into parts with their own titles ("For the sauce:"), keep each title as its own entry written "## For the sauce" before that part's steps. Never put a title in a step. No headings when the method is one part.
- Title: the recipe's own name if it is stated in the text; otherwise a short, plain title describing the dish.
- Servings: only if the text states a serving count or yield, otherwise null.

PASTED TEXT:
{text}
"""


def from_pasted_text(d: Path, fx: dict, log: dict):
    text = fx.get("pasted_text", "")
    r = ollama(PASTE_PROMPT.format(meals=MEALS, text=text[:8000]), num_ctx=8192, timeout=300)
    r, dropped = finish(r, text, fx.get("page_title") or "Untitled")
    log["paste_chars"], log["dropped"] = len(text), dropped
    return r


ING_HEAD = re.compile(r"\W*(ingredients?|what you.?ll need|you will need)\W*", re.I)


def page_text(html: str) -> str:
    import trafilatura
    t = trafilatura.extract(html, include_tables=True, include_comments=False, favor_recall=True,
                            output_format="txt")
    if not t:  # last resort: crude tag strip
        t = re.sub(r"(?s)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
        t = re.sub(r"<[^>]+>", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def recipe_window(text: str, limit=7000) -> str:
    """Long blog posts bury the recipe card; start a little above the first Ingredients heading."""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if len(l) < 40 and ING_HEAD.fullmatch(l.strip()):
            return "\n".join(lines[max(0, i - 2):])[:limit]
    return text[:limit]


NOTE_BOXES = ("wprm-recipe-notes", "tasty-recipes-notes", "mv-create-notes", "recipe-notes", "recipe__notes", "o-ChefNotes")
NOTE_TITLE = re.compile(r"(cook.s |chef.s |recipe )?notes?:?", re.I)


def extract_notes(html: str):
    """The recipe's own notes section ("(see notes)" points here), as a list of paragraphs. Not in schema.org data."""
    try:
        import lxml.html
        doc = lxml.html.fromstring(html)
    except Exception:
        return []
    box = None
    for name in NOTE_BOXES:
        found = doc.xpath(f"//*[contains(concat(' ', normalize-space(@class), ' '), ' {name} ') or "
                          f"contains(concat(' ', normalize-space(@class), ' '), ' {name}-body ')]")
        found = [f for f in found if "private" not in (f.get("class") or "")]
        if found:
            box = found[-1] if name == "wprm-recipe-notes" else found[0]
            break
    if box is None:
        return []
    for bad in box.xpath(".//script|.//style|.//button|.//form|.//*[contains(@class,'private-notes')]"):
        bad.getparent().remove(bad)
    for el in box.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("p", "li", "br", "h2", "h3", "h4") or "spacer" in (el.get("class") or "") \
                or "display: block" in (el.get("style") or ""):
            el.text = "\n" + (el.text or "")
            el.tail = "\n" + (el.tail or "")
    out = []
    for line in box.text_content().split("\n"):
        line = re.sub(r"\s+", " ", unescape(line)).strip()
        if line and not NOTE_TITLE.fullmatch(line):
            out.append(line[:2000])
    return out[:30]


NOTE_REF = re.compile(r"\(see (the |recipe |cook.s |chef.s )?notes?\b|\bsee (the )?(recipe |cook.s |chef.s )?notes? (below|above)", re.I)


def finish(r, corpus, title_fallback="", evidence_min=60):
    """Shared checks: no invented numbers, flag thin or amount-free recipes."""
    r["title"] = re.sub(r"\s+", " ", unescape(r.get("title") or "")).strip() or title_fallback or "Untitled"
    r["ingredients"] = norm_ingredients(clean_list(r.get("ingredients")))
    r["steps"] = norm_steps(clean_list(r.get("steps")))
    r["ingredients"], r["steps"], dropped, changes = ground(r["ingredients"], r["steps"], corpus)
    if r.get("servings") and not (_nums(str(r["servings"])) & _nums(corpus)):
        r["servings"] = None
    r["needs_review"] = None
    r["unverified"] = []
    if not r["ingredients"] or not r["steps"] or len(corpus.split()) < evidence_min:
        r["ingredients"], r["steps"] = [], []
        r["needs_review"] = "source has too little recipe detail"
    elif dropped:
        r["needs_review"] = f"removed {dropped} unverified quantities"
        r["unverified"] = changes
    elif not any(re.search(r"\d", i) for i in r["ingredients"]):
        r["needs_review"] = "source gives no amounts"
    if r.get("meal") not in MEALS:
        r["meal"] = "Other"
    return r, dropped


def from_page_text(d: Path, fx: dict, log: dict):
    html = (d / "page.html").read_text(errors="replace", encoding="utf-8")
    text = page_text(html)
    (d / "page.txt").write_text(text, encoding="utf-8")
    window = recipe_window(text)
    r = ollama(PAGE_PROMPT.format(meals=MEALS, title=fx.get("page_title") or "", text=window),
               num_ctx=8192, timeout=300)
    r, dropped = finish(r, fx.get("page_title", "") + " " + window, fx.get("page_title") or fx["source_url"])
    log["page_chars"], log["dropped"] = len(text), dropped
    return r


NUT_LABELS = ["Serving size", "Calories", "Fat", "Saturated fat", "Carbohydrates", "Fiber", "Sugar",
              "Protein", "Cholesterol", "Sodium"]
LD_NUT = {"servingSize": "Serving size", "calories": "Calories", "fatContent": "Fat",
          "saturatedFatContent": "Saturated fat", "carbohydrateContent": "Carbohydrates",
          "fiberContent": "Fiber", "sugarContent": "Sugar", "proteinContent": "Protein",
          "cholesterolContent": "Cholesterol", "sodiumContent": "Sodium"}
NUT_HINT = re.compile(r"nutrition|calories|kcal", re.I)


def _clean_val(v):
    return re.sub(r"\s+", " ", str(v)).strip()[:40]


def nutrition_from_ld(ld):
    """schema.org NutritionInformation -> {label: "405 kcal", ...} or None."""
    n = ld.get("nutrition") or {}
    if not isinstance(n, dict):
        return None
    out = {}
    for k, label in LD_NUT.items():
        v = n.get(k)
        if v in (None, "") or not re.search(r"\d", str(v)):
            continue
        v = _clean_val(v)
        if label == "Calories" and re.fullmatch(r"[\d.,]+", v):
            v += " kcal"
        out[label] = v
    return out if any(k != "Serving size" for k in out) else None


NUT_PROMPT = """Extract the nutrition facts stated in the text below. Use ONLY the text. Never estimate or calculate.
Output JSON with exactly these keys: {keys}
Each value is the number with its unit exactly as written (for example "405 kcal", "18 g", "843 mg"). Use null for anything not stated. If the text has no nutrition facts, every value is null.

TEXT:
{text}
"""


def extract_nutrition(corpus):
    """Nutrition facts found in free text (page or video caption). Every number must appear in the text."""
    m = NUT_HINT.search(corpus or "")
    if not m:
        return None
    text = corpus[max(0, m.start() - 300): m.start() + 1500]
    try:
        j = ollama(NUT_PROMPT.format(keys=NUT_LABELS, text=text), num_ctx=4096, timeout=120)
    except Exception:
        return None
    have, out = _nums(text), {}
    for label in NUT_LABELS:
        v = j.get(label)
        if v in (None, ""):
            continue
        v = _clean_val(v)
        if _nums(v) and _nums(v) <= have:
            out[label] = v
    return out if any(k != "Serving size" for k in out) else None


def servings_number(s):
    m = re.search(r"\d+", str(s or ""))
    n = int(m.group()) if m else 0
    return n if 1 <= n <= 100 else None


SERV_PROMPT = """How many servings does this recipe make? Judge from the ingredient amounts and the kind of dish, as a typical home cook would serve it.
Return JSON {{"servings": <whole number from 1 to 24>}}.
Title: {title}
Ingredients: {ing}
"""


def add_servings(r):
    """servings_num = stated count if the source gave one, otherwise a model estimate (servings_inferred)."""
    n = servings_number(r.get("servings"))
    r["servings_inferred"] = False
    if not n and r.get("ingredients"):
        try:
            j = ollama(SERV_PROMPT.format(title=r.get("title"), ing=r["ingredients"][:30]),
                       num_ctx=2048, timeout=120)
            n = servings_number(j.get("servings"))
            r["servings_inferred"] = bool(n)
        except Exception:
            n = None
    r["servings_num"] = n


TIME_KEYS = ("prep_min", "cook_min", "total_min")  # active_min is kept apart: it is read from the page, never guessed
TIME_HINT = re.compile(r"prep(?:aration)?\s*time|cook(?:ing)?\s*time|total\s*time|\bprep\s*:|\bcook\s*:|\btotal\s*:|\bactive\s*:", re.I)


def iso_minutes(s):
    """schema.org duration (PT1H30M) -> whole minutes or None."""
    # Also the long form some sites write ("P0Y0M0DT0H35M0.000S"): years/months only when zero, seconds may be fractional
    m = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:\.\d+)?S)?)?", str(s or "").strip())
    if not m:
        return None
    y, mo, w, d, h, mi = (int(x or 0) for x in m.groups())
    if y or mo:
        return None
    n = w * 10080 + d * 1440 + h * 60 + mi
    return n if 1 <= n <= 2880 else None


TIMES_PROMPT = """Extract the preparation, cooking and total time stated in the text below. Use ONLY the text; never estimate or add up times.
Output JSON {{"prep_min": int or null, "cook_min": int or null, "total_min": int or null}} with each value in whole minutes (1 hr 30 min = 90). Use null for anything not stated.

TEXT:
{text}
"""


def _time_ok(v, have):
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 2880:
        return False
    h, m = divmod(v, 60)
    return str(v) in have or bool(h and not m and str(h) in have) or bool(h and m and str(h) in have and str(m) in have)


def extract_times(corpus):
    """Prep/cook/total minutes, only when the text labels them; numbers must appear in the text."""
    out = {k: None for k in TIME_KEYS}
    m = TIME_HINT.search(corpus or "")
    if not m:
        return out
    text = corpus[max(0, m.start() - 100): m.start() + 600]
    try:
        j = ollama(TIMES_PROMPT.format(text=text), num_ctx=2048, timeout=120)
    except Exception:
        return out
    have = _nums(text)
    for k in out:
        v = j.get(k)
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if _time_ok(v, have):
            out[k] = v
    return out


def gen_tags(r):
    return make_tags(r, lambda prompt: ollama(prompt, num_ctx=2048, timeout=120))


def stage(d: Path, key: str, note: str = ""):
    """Tell the app which step we are on (shown live in the UI)."""
    cur = _job.get()
    if cur is not None and key != "waiting":
        cur[1] = key  # remembered so the step comes back after a wait in the AI queue
    try:
        (d / ".stage").write_text(key, encoding="utf-8")
        note_file = d / ".stage_note"
        if note:
            note_file.write_text(note, encoding="utf-8")
        else:
            note_file.unlink(missing_ok=True)
    except Exception:
        pass


def process(d: Path):
    _job.set([d, "starting"])
    t0 = time.time()
    fx = json.loads((d / "fetch.json").read_text(encoding="utf-8"))
    log = {}
    nut_corpus = None  # text searched for nutrition facts when the structured data has none
    if fx["kind"] == "page":
        if fx.get("has_jsonld") and not os.environ.get("FORCE_TEXT"):
            stage(d, "reading")
            r = from_jsonld(d)
            r["meal"] = guess_meal(r["title"], r["ingredients"], r.pop("category_hint"))
            r["needs_review"] = None
        else:
            stage(d, "writing")
            r = from_page_text(d, fx, log)
            nut_corpus = (d / "page.txt").read_text(encoding="utf-8")
    elif fx["kind"] == "text":
        stage(d, "writing")
        r = from_pasted_text(d, fx, log)
        nut_corpus = fx.get("pasted_text", "")
    else:
        video = d / "source.mp4"
        stage(d, "transcribing")
        transcript = transcribe(video)
        log["transcribe_s"] = round(time.time() - t0, 1)
        stage(d, "scanning")
        frames = extract_frames(video, d / "_frames")
        scan = scan_frames(frames)
        log["scan_s"] = round(time.time() - t0, 1)
        (d / "ocr.json").write_text(json.dumps(scan, indent=1, ensure_ascii=False), encoding="utf-8")
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")
        ocr = dedupe_text(s["text"] for s in scan)
        stage(d, "writing")
        r = ollama(STRUCT_PROMPT.format(meals=MEALS, caption=fx.get("caption", "")[:3000],
                                        transcript=transcript[:4000], ocr=ocr[:4000]),
                   num_ctx=8192, timeout=300)
        keep_caption_amounts(r, caption_ingredients(fx.get("caption", "")))
        fill_caption_amounts(r, fx.get("caption", ""))
        corpus = " ".join([fx.get("caption", ""), transcript, ocr])
        r, dropped = finish(r, corpus, fx.get("yt_title") or fx["source_url"])
        nut_corpus = corpus
        log["dropped"], log["evidence_words"] = dropped, len(corpus.split())
        for f in (d / "_frames").glob("f_*.jpg"):
            f.unlink()
        (d / "_frames").rmdir()
    stage(d, "finishing")
    no_times = not any(r.get(k) for k in TIME_KEYS)
    if (not r.get("nutrition") or no_times) and nut_corpus is None and (d / "page.html").exists():
        nut_corpus = page_text((d / "page.html").read_text(errors="replace", encoding="utf-8"))
    if not r.get("nutrition"):
        r["nutrition"] = extract_nutrition(nut_corpus)
    if no_times:
        r.update(extract_times(nut_corpus))
    for k in TIME_KEYS:
        r.setdefault(k, None)
    add_servings(r)
    r["tags"] = gen_tags(r)
    r.setdefault("favorite", False)
    r.setdefault("notes", "")
    extras = r.pop("page_extras", [])
    if (d / "page.html").exists():
        r["recipe_notes"] = extract_notes((d / "page.html").read_text(errors="replace", encoding="utf-8")) + extras
    if fx.get("redirected_elsewhere") and not r.get("needs_review"):
        r["needs_review"] = f"the link now redirects to a different page: {fx.get('final_url')}"
    if r.get("active_min") and r.get("cook_min") == r["active_min"] and not r.get("prep_min"):
        r["cook_min"] = None  # a page's Active time is not its cooking time
    if not r.get("recipe_notes") and not r.get("needs_review") and NOTE_REF.search(" ".join(r.get("steps") or [])):
        r["needs_review"] = "a step refers to a recipe note that was not found on the page"
    cover = "thumbnail.jpg" if (d / "thumbnail.jpg").exists() else None
    r.update(source_url=fx["source_url"], kind=fx["kind"], cover=cover, cover_url=None if cover else fx.get("cover_url"),
             has_video=(d / "source.mp4").exists(), processed_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    if fx["kind"] == "text":
        r["source_note"] = "Pasted recipe"
        r["paste_hash"] = hashlib.sha1(re.sub(r"\s+", " ", fx.get("pasted_text", "").strip().lower()).encode()).hexdigest()
    (d / "recipe.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    log["total_s"] = round(time.time() - t0, 1)
    return r, log


if __name__ == "__main__":
    for p in sys.argv[1:]:
        try:
            r, log = process(Path(p))
            print("OK", p, log, flush=True)
        except Exception as e:
            print("FAIL", p, repr(e), flush=True)
