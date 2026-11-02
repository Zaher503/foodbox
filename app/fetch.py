"""Step 2: fetch a link into a recipe folder. No AI here."""
import hashlib
import html as htmllib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import urllib.error
from urllib.parse import urljoin, urlparse

# Data lives in $FOODBOX_DATA (default: ./data next to the repo)
BASE = Path(os.environ.get("FOODBOX_DATA") or Path(__file__).resolve().parent.parent / "data")
DATA = BASE / "recipes"
YTDLP = [sys.executable, "-m", "yt_dlp"]


def ytdlp_for(url: str) -> list:
    """TikTok serves a verification page to a plain Python client, so ask yt-dlp to present itself as Chrome
    (needs curl_cffi; without it the call is made as before)."""
    if "tiktok.com" in (urlparse(url).hostname or "") and importlib.util.find_spec("curl_cffi"):
        return [*YTDLP, "--impersonate", "chrome"]
    return YTDLP

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
VIDEO_HOSTS = ("tiktok.com", "youtube.com", "youtu.be", "vm.tiktok.com")


def check_url(url: str) -> str:
    """Reject non-http(s) and anything resolving to a private/LAN address."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only http(s) links are accepted")
    try:
        addrs = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise ValueError(f"the website {p.hostname} could not be found (the site may be gone, or the link has a typo)") from None
    for fam, _, _, _, sa in addrs:
        ip = ipaddress.ip_address(sa[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("link resolves to a private address")
    return url.strip()


def is_video(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS)


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:50] or "recipe"


def make_dir(url: str, title: str) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:6]
    d = DATA / f"{datetime.now():%Y-%m-%d}_{slugify(title)}_{h}"
    (d / "shots").mkdir(parents=True, exist_ok=True)
    return d


def make_thumbs(d: Path, n: int = 8) -> list:
    """Stills spread evenly across the video, kept in thumbs/ as cover candidates."""
    video = d / "source.mp4"
    try:
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
            check=True, capture_output=True, text=True, timeout=60).stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    out = d / "thumbs"
    out.mkdir(exist_ok=True)
    for old in out.glob("t_*.jpg"):
        old.unlink()
    made = []
    for i in range(n):
        f = out / f"t_{i + 1:02d}.jpg"
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{dur * (i + 0.5) / n:.2f}", "-i", str(video),
                            "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", str(f)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if f.exists() and f.stat().st_size:
            made.append(f.name)
    return made


def fetch_video(url: str) -> Path:
    info = json.loads(subprocess.run(
        [*ytdlp_for(url), "--no-warnings", "--skip-download", "--dump-single-json", url],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout)
    d = make_dir(url, info.get("title") or info.get("id"))
    subprocess.run(
        [*ytdlp_for(url), "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "-o", str(d / "source.%(ext)s"),
         "--write-thumbnail", "--convert-thumbnails", "jpg",
         "--write-auto-subs", "--sub-langs", "en.*", "--sub-format", "vtt",
         url], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    thumb = next(d.glob("source.jpg"), None)
    if thumb:
        thumb.rename(d / "thumbnail.jpg")
    meta = {
        "kind": "video", "source_url": url, "site": info.get("extractor"),
        "uploader": info.get("uploader"), "duration": info.get("duration"),
        "caption": info.get("description") or "", "yt_title": info.get("title"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    (d / "fetch.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    make_thumbs(d)  # a failure here never fails the import: the site thumbnail is still the cover
    return d


class _LD(HTMLParser):
    def __init__(self):
        super().__init__()
        self.on, self.blocks = False, []

    def handle_starttag(self, tag, attrs):
        self.on = tag == "script" and dict(attrs).get("type") == "application/ld+json"
        if self.on:
            self.blocks.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.on = False

    def handle_data(self, data):
        if self.on:
            self.blocks[-1] += data


def _find_recipe(node):
    if isinstance(node, list):
        for n in node:
            r = _find_recipe(n)
            if r:
                return r
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
            return node
        return _find_recipe(node.get("@graph", []))
    return None


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # a redirect must not lead to a LAN address
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)
MAX_BYTES = 8_000_000


def _http_error(code: int) -> str:
    if code in (404, 410):
        return f"page not found (HTTP {code}): the link may be dead"
    return f"site refused the download (HTTP {code})"


def _open(url: str, limit=MAX_BYTES):
    """(body, final url after redirects)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    try:
        resp = _opener.open(req, timeout=30)
        return resp.read(limit), resp.geturl()
    except urllib.error.HTTPError as e:
        raise RuntimeError(_http_error(e.code)) from None


def _get(url: str, limit=MAX_BYTES) -> bytes:
    return _open(url, limit)[0]


SLUG_STOP = {"the", "and", "with", "for", "recipe", "recipes", "best", "easy", "how", "make"}


def _slug_words(url: str) -> set:
    """Words of the last path part that has any ("minted-peas-rice-with-feta" -> {minted, pea, rice, feta})."""
    for seg in reversed([s for s in urlparse(url).path.split("/") if s]):
        words = {w[:-1] if w.endswith("s") else w for w in re.findall(r"[a-z]{3,}", seg.lower()) if w not in SLUG_STOP}
        if words:
            return words
    return set()


def redirected_elsewhere(requested: str, final: str) -> bool:
    """True when a redirect led to a page whose address shares too few words with the one asked for: old links
    often redirect to an unrelated recipe or the home page, and that must not be saved as if it were the link."""
    if not final or urlparse(requested).path.rstrip("/") == urlparse(final).path.rstrip("/"):
        return False
    want = _slug_words(requested)
    return bool(want) and len(want & _slug_words(final)) / len(want) < 0.6


def _get_browser(url: str) -> bytes:
    """Fallback for Cloudflare-style challenges: render in a stealth Chromium (under Xvfb on Linux)."""
    import subprocess, tempfile
    here = Path(__file__).parent
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "page.html"
        try:
            xvfb = ["xvfb-run", "-a"] if shutil.which("xvfb-run") else []
            # Non-root Chromium crashes (crashpad) unless it has writable XDG dirs
            env = {**os.environ, "XDG_CONFIG_HOME": td, "XDG_CACHE_HOME": td}
            subprocess.run([*xvfb, sys.executable, str(here / "browser_fetch.py"), url, str(out)],
                           check=True, timeout=120, capture_output=True, env=env)
        except Exception:
            raise RuntimeError("site refused the download (HTTP 403, browser fallback also blocked)") from None
        return out.read_bytes()[:MAX_BYTES]


def _meta(html: str, prop: str):
    for pat in (rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{prop}["\']'):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1).strip()


def _usable(recipe) -> bool:
    return bool(recipe and recipe.get("recipeIngredient") and recipe.get("recipeInstructions"))


MIN_COVER_PX = 600
SIZE_IN_URL = re.compile(r"[-_.](\d{2,4})x(\d{2,4})(?=[./_-]|$)")


def _image_candidates(html: str, recipe, base_url: str) -> list:
    """[(url, pixels)] from the recipe's schema.org image(s), og:image and twitter:image, biggest first.
    Pixels come from the ImageObject width/height or a "-225x225" style size in the file name; no hint
    means a site original, which counts as big (WordPress lists the small crops first)."""
    raw = []
    img = (recipe or {}).get("image")
    for i in (img if isinstance(img, list) else [img]):
        if isinstance(i, dict):
            w, h = str(i.get("width") or ""), str(i.get("height") or "")
            raw.append((i.get("url") or i.get("contentUrl"), int(w) * int(h) if w.isdigit() and h.isdigit() else None))
        else:
            raw.append((i, None))
    raw += [(_meta(html, "og:image"), None), (_meta(html, "twitter:image"), None)]
    out, seen = [], set()
    for u, px in raw:
        if not isinstance(u, str) or not u.strip():
            continue
        u = urljoin(base_url, u.strip())
        if u in seen:
            continue
        seen.add(u)
        if px is None:
            m = SIZE_IN_URL.search(u)
            px = int(m.group(1)) * int(m.group(2)) if m else 10**8
        out.append((u, px))
    return sorted(out, key=lambda c: -c[1])  # stable: equal sizes keep the page's own order


IMAGE_MAGIC = (bytes([0xFF, 0xD8, 0xFF]), bytes([0x89]) + b"PNG", b"GIF8")


def _looks_like_image(b: bytes) -> bool:
    """JPEG, PNG, GIF, WebP or AVIF/HEIC bytes (an HTML error page saved as a cover is not one)."""
    return len(b) > 4000 and (b.startswith(IMAGE_MAGIC) or (b[:4] == b"RIFF" and b[8:12] == b"WEBP") or b[4:12] in (b"ftypavif", b"ftypheic"))


def save_cover(d: Path, url: str, html: str, recipe):
    """Download the best cover into thumbnail.jpg. Returns (remote_url, error): when every download fails the
    page's own picture URL is kept (the browser can usually load it even where the server is refused) and the
    reason is written to fetch.json instead of being swallowed."""
    cands = _image_candidates(html, recipe, url)
    big = [c for c in cands if c[1] >= MIN_COVER_PX ** 2] or cands  # skip thumbnails unless nothing else is left
    errors = []
    for u, _ in big[:4]:
        try:
            data = _get(check_url(u), 10_000_000)
        except Exception as e:
            errors.append(f"{urlparse(u).path.rsplit('/', 1)[-1][:40]}: {e}")
            continue
        if _looks_like_image(data):
            (d / "thumbnail.jpg").write_bytes(data)
            return None, None
        errors.append(f"{urlparse(u).path.rsplit('/', 1)[-1][:40]}: not an image")
    return (big[0][0] if big else None), ("; ".join(errors)[:300] or ("no picture found on the page" if not cands else None))


def fetch_page(url: str) -> Path:
    try:
        raw, final = _open(url)
        html = raw.decode("utf-8", "replace")
    except RuntimeError as e:
        if "HTTP 403" not in str(e):
            raise
        html, final = _get_browser(url).decode("utf-8", "replace"), url
    p = _LD()
    p.feed(html)
    recipe = None
    for b in p.blocks:
        try:
            recipe = _find_recipe(json.loads(b))
        except json.JSONDecodeError:
            continue
        if recipe:
            break
    title = htmllib.unescape((recipe or {}).get("name") or _meta(html, "og:title") or urlparse(url).path)
    d = make_dir(url, title)
    (d / "page.html").write_text(html, encoding="utf-8")
    (d / "jsonld.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
    cover_url, cover_error = save_cover(d, url, html, recipe)
    (d / "fetch.json").write_text(json.dumps({
        "kind": "page", "source_url": url, "has_jsonld": _usable(recipe), "page_title": title,
        "cover_url": cover_url, "cover_error": cover_error,
        "final_url": final if final != url else None, "redirected_elsewhere": redirected_elsewhere(url, final),
        "fetched_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
    return d


def fetch_text(text: str) -> Path:
    """A recipe pasted in directly, no link. No download here; the model structures it in process.py."""
    text = text.strip()
    title = next((l.strip() for l in text.splitlines() if l.strip()), "pasted recipe")
    h = hashlib.sha1(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()[:6]
    d = DATA / f"{datetime.now():%Y-%m-%d}_{slugify(title)}_{h}"
    (d / "shots").mkdir(parents=True, exist_ok=True)
    meta = {"kind": "text", "source_url": "", "pasted_text": text, "page_title": title[:200],
            "fetched_at": datetime.now(timezone.utc).isoformat()}
    (d / "fetch.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return d


def fetch(url: str) -> Path:
    url = check_url(url)
    return fetch_video(url) if is_video(url) else fetch_page(url)


if __name__ == "__main__":
    for u in sys.argv[1:]:
        try:
            print("OK  ", fetch(u), flush=True)
        except Exception as e:
            print("FAIL", u, repr(e), getattr(e, "stderr", ""), flush=True)
