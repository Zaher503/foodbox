"""Step 2: fetch a link into a recipe folder. No AI here."""
import hashlib
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
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
VIDEO_HOSTS = ("tiktok.com", "youtube.com", "youtu.be", "vm.tiktok.com")


def check_url(url: str) -> str:
    """Reject non-http(s) and anything resolving to a private/LAN address."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("only http(s) links are accepted")
    for fam, _, _, _, sa in socket.getaddrinfo(p.hostname, None):
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


def fetch_video(url: str) -> Path:
    info = json.loads(subprocess.run(
        [*YTDLP, "--no-warnings", "--skip-download", "--dump-single-json", url],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout)
    d = make_dir(url, info.get("title") or info.get("id"))
    subprocess.run(
        [*YTDLP, "--no-warnings", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
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


def _get(url: str, limit=MAX_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    try:
        return _opener.open(req, timeout=30).read(limit)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"site refused the download (HTTP {e.code})") from None


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


def fetch_page(url: str) -> Path:
    try:
        html = _get(url).decode("utf-8", "replace")
    except RuntimeError as e:
        if "HTTP 403" not in str(e):
            raise
        html = _get_browser(url).decode("utf-8", "replace")
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
    title = (recipe or {}).get("name") or _meta(html, "og:title") or urlparse(url).path
    d = make_dir(url, title)
    (d / "page.html").write_text(html, encoding="utf-8")
    (d / "jsonld.json").write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")
    img = (recipe or {}).get("image")
    if isinstance(img, list):
        img = img[0]
    if isinstance(img, dict):
        img = img.get("url")
    img = img or _meta(html, "og:image")
    if img:
        try:
            (d / "thumbnail.jpg").write_bytes(_get(check_url(urljoin(url, img)), 10_000_000))
        except Exception:
            pass  # a missing cover is not fatal
    (d / "fetch.json").write_text(json.dumps({
        "kind": "page", "source_url": url, "has_jsonld": _usable(recipe), "page_title": title,
        "fetched_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
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
