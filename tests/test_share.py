import importlib
import json
import os
import sys

import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")


@pytest.fixture(scope="module")
def ctx():
    os.environ["RECIPE_KEY"] = "test-key"
    sys.path.insert(0, APP)
    from fastapi.testclient import TestClient
    main = importlib.import_module("main")
    d = main.RECIPES / "2026-01-01_share-test_abc"
    (d / "shots").mkdir(parents=True, exist_ok=True)
    (d / "shots" / "user_1.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    (d / "recipe.json").write_text(json.dumps({
        "title": "Shared Soup", "meal": "Dinner", "servings": "4", "ingredients": ["2 cups broth"],
        "steps": ["Add [[Pesto|2025-01-01_pesto_x]].", "Simmer."], "cover": "shots/user_1.jpg",
        "notes": "private note", "added_by": "someone", "favorite": True, "source_url": "https://example.com/soup",
    }), encoding="utf-8")
    return TestClient(main.app), d.name


def test_share_flow(ctx):
    c, rid = ctx
    assert c.get("/s/AAAAAAAAAAAAAAAAAAAAAA/recipe").status_code == 404
    r = c.post(f"/api/recipes/{rid}/share").json()
    tok = r["token"]
    assert r["path"] == f"/s/{tok}"
    assert c.post(f"/api/recipes/{rid}/share").json()["token"] == tok  # same link every time

    page = c.get(f"/s/{tok}")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "script-src 'self'" in page.headers["content-security-policy"]

    data = c.get(f"/s/{tok}/recipe").json()
    assert data["title"] == "Shared Soup"
    assert data["steps"][0] == "Add Pesto."  # links to unshared recipes become plain text
    for k in ("notes", "added_by", "favorite", "share_token", "id"):
        assert k not in data
    assert data["cover"] == f"/s/{tok}/cover"
    assert c.get(f"/s/{tok}/cover").status_code == 200
    assert c.get("/s/assets/kit.js").status_code == 200
    assert c.get("/s/assets/share.js").status_code == 200
    assert c.get("/s/assets/main.py").status_code == 404

    # the public routes are read-only
    for m in ("post", "patch", "put", "delete"):
        assert getattr(c, m)(f"/s/{tok}/recipe").status_code == 405

    assert c.delete(f"/api/recipes/{rid}/share").status_code == 200
    assert c.get(f"/s/{tok}").status_code == 404
    assert c.get(f"/s/{tok}/recipe").status_code == 404
    assert c.get(f"/s/{tok}/cover").status_code == 404


def test_bad_tokens(ctx):
    c, _ = ctx
    for t in ("short", "../../etc/passwd", "a" * 100):
        assert c.get(f"/s/{t}/recipe").status_code == 404
