"""Adding a recipe from photos of a cookbook or magazine page."""
import importlib
import io
import json
import os
import sys

import pytest
from PIL import Image

APP = os.path.join(os.path.dirname(__file__), "..", "app")

PAGE_TEXT = """Lemon Butter Pasta
Serves 4
Ingredients
400 g spaghetti
3 tbsp butter
1 lemon, zested and juiced
50 g parmesan, grated
Method
1. Cook the spaghetti in salted water for 10 minutes.
2. Melt the butter, add the lemon zest and juice.
3. Toss the pasta with the butter and parmesan. Season and serve.
12 | MAGAZINE NAME"""


def jpeg(color=(200, 180, 150), size=(60, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG")
    return out.getvalue()


@pytest.fixture(scope="module")
def main():
    os.environ["RECIPE_KEY"] = "test-key"
    sys.path.insert(0, APP)
    return importlib.import_module("main")


@pytest.fixture
def client(main, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "run_job", lambda job: None)  # keep queued jobs from running in the background worker
    return TestClient(main.app)


def post(client, *blobs, note="", names=None):
    names = names or [f"p{i}.jpg" for i in range(len(blobs))]
    files = [("files", (n, b, "image/jpeg")) for n, b in zip(names, blobs)]
    return client.post("/api/ui/photo", files=files, data={"note": note})


def test_rejects_non_images(client):
    r = client.post("/api/ui/photo", files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert r.status_code == 400


def test_rejects_more_than_four_pages(client):
    assert post(client, *[jpeg((i, 0, 0)) for i in range(5)]).status_code == 400


def test_unreadable_image_leaves_no_folder(client, main):
    before = set(main.RECIPES.iterdir())
    r = post(client, b"not really a jpeg")
    assert r.status_code == 400
    assert set(main.RECIPES.iterdir()) == before


def test_photos_are_saved_and_queued_once(client, main):
    a, b = jpeg((10, 20, 30), (1200, 3000)), jpeg((30, 20, 10))
    r = post(client, a, b, note="  Simple   Suppers, p. 12 ")
    assert r.status_code == 200 and r.json()["status"] == "queued"
    job = main.q("SELECT recipe_id FROM jobs WHERE id=?", r.json()["job"]).fetchone()
    d = main.RECIPES / job["recipe_id"]
    fx = json.loads((d / "fetch.json").read_text(encoding="utf-8"))
    assert fx["kind"] == "photo" and fx["pages"] == ["page_1.jpg", "page_2.jpg"]
    assert fx["source_note"] == "Simple Suppers, p. 12" and fx["cover_url"] == "/icons/scan-cover.svg"
    with Image.open(d / "shots" / "page_1.jpg") as im:
        assert max(im.size) == 2400  # big photos are capped, small print stays readable
    again = post(client, a, b)
    assert again.json() == {"status": "queued", "job": r.json()["job"]}


def test_generic_cover_is_served(client):
    r = client.get("/icons/scan-cover.svg")
    assert r.status_code == 200 and "svg" in r.headers["content-type"]


def test_process_reads_pages_then_checks_numbers(main, monkeypatch, tmp_path):
    import fetch
    import process

    monkeypatch.setattr(fetch, "DATA", tmp_path)
    d = fetch.fetch_photos([jpeg()], "")
    calls = []

    def fake_ollama(prompt, images=None, num_ctx=8192, fmt="json", timeout=300):
        calls.append(fmt)
        if fmt is None:  # page reading returns plain text
            assert images and str(images[0]).endswith("page_1.jpg")
            return PAGE_TEXT
        if "TEXT READ FROM THE PHOTOS" in prompt:
            assert "400 g spaghetti" in prompt
            return {"title": "Lemon Butter Pasta", "meal": "Dinner", "servings": "4",
                    "ingredients": ["400 g spaghetti", "3 tbsp butter", "1 lemon, zested and juiced",
                                    "50 g parmesan, grated", "2 cloves garlic"],
                    "steps": ["Cook the spaghetti for 15 minutes.", "Melt the butter, add the lemon.",
                              "Toss with the parmesan and serve."]}
        return {}

    monkeypatch.setattr(process, "ollama", fake_ollama)
    r, log = process.process(d)
    assert calls[0] is None
    assert r["kind"] == "photo" and r["source_note"] == "Scanned from a photo"
    assert r["cover"] is None and r["cover_url"] == "/icons/scan-cover.svg"
    assert r["title"] == "Lemon Butter Pasta" and r["servings"] == "4"
    assert "400 g spaghetti" in r["ingredients"]
    assert not any("15" in s for s in r["steps"])  # 15 is nowhere in the page text
    assert r["needs_review"] and "unverified" in r["needs_review"]
    assert (d / "page.txt").read_text(encoding="utf-8").startswith("Lemon Butter Pasta")
    assert json.loads((d / "ocr.json").read_text(encoding="utf-8"))[0]["page"] == "page_1.jpg"


def test_unreadable_photo_fails_with_a_clear_message(main, monkeypatch, tmp_path):
    import fetch
    import process

    monkeypatch.setattr(fetch, "DATA", tmp_path)
    d = fetch.fetch_photos([jpeg((1, 2, 3))], "")
    monkeypatch.setattr(process, "ollama", lambda *a, **k: "" if k.get("fmt", "json") is None else {})
    with pytest.raises(RuntimeError, match="could not read the text"):
        process.process(d)


def test_strips_overlap_is_not_repeated():
    sys.path.insert(0, APP)
    import process
    assert process.join_strips(["Ingredients\n1 cup flour\n2 eggs", "1 cup flour\n2 eggs\n1 tsp salt"]) == \
        "Ingredients\n1 cup flour\n2 eggs\n1 tsp salt"
