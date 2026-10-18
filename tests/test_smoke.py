import importlib
import os
import sys

import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    os.environ["FOODBOX_DATA"] = str(tmp_path_factory.mktemp("data"))
    os.environ["RECIPE_KEY"] = "test-key"
    sys.path.insert(0, APP)
    from fastapi.testclient import TestClient
    main = importlib.import_module("main")
    return TestClient(main.app)


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "html" in r.headers["content-type"]


def test_add_requires_key(client):
    r = client.post("/api/add", json={"url": "https://example.com/x"})
    assert r.status_code == 401


def test_add_rejects_private_address(client):
    r = client.post("/api/add", json={"url": "http://192.168.0.1/x"}, headers={"X-Recipe-Key": "test-key"})
    assert r.status_code == 400


def test_library_empty(client):
    r = client.get("/api/recipes")
    assert r.status_code == 200


def test_paste_rejects_short_text(client):
    r = client.post("/api/ui/paste", json={"text": "chicken soup"})
    assert r.status_code == 400


def test_paste_rejects_empty(client):
    r = client.post("/api/ui/paste", json={})
    assert r.status_code == 400


def test_paste_queues_a_job(client):
    text = ("Grandma's Chicken Soup\n\nIngredients:\n- 2 lbs chicken thighs\n- 1 onion, diced\n"
            "- 3 carrots, sliced\n- 8 cups chicken broth\n\nDirections:\n1. Combine everything in a pot.\n"
            "2. Simmer for 1 hour.\n3. Season to taste and serve.")
    r = client.post("/api/ui/paste", json={"text": text})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["job"]


def test_paste_of_an_already_saved_recipe_returns_it(client):
    import importlib
    main = importlib.import_module("main")
    text = "B" * 120
    d = main.RECIPES / "test-existing-paste"
    d.mkdir(parents=True, exist_ok=True)
    main.save(d, {"title": "Already saved", "meal": "Other", "ingredients": ["1 egg"], "steps": ["Cook it."],
                  "source_url": "", "kind": "text", "paste_hash": main.text_hash(text)})
    r = client.post("/api/ui/paste", json={"text": text})
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "exists", "recipe_id": "test-existing-paste"}
