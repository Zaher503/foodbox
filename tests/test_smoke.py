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
