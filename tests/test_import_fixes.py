import json
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import fetch  # noqa: E402
import process as P  # noqa: E402

JPEG = bytes([0xFF, 0xD8, 0xFF]) + b"x" * 5000


def test_iso_minutes_long_form():
    assert P.iso_minutes("P0Y0M0DT0H35M0.000S") == 35
    assert P.iso_minutes("P0Y0M0DT1H45M0.000S") == 105
    assert P.iso_minutes("PT1H30M") == 90
    assert P.iso_minutes("P1M") is None  # a month is not minutes


def test_time_hint_finds_total_and_active():
    assert P.TIME_HINT.search("Total: 1 hr 45 min (includes resting time) Active: 35 min")


def test_zero_width_characters_removed():
    assert P.unescape("​Press the button") == "Press the button"
    assert P.clean_list(["​Press &amp; go"]) == ["Press & go"]


def test_missing_host_message():
    def gone(*a, **k):
        raise socket.gaierror(-5, "No address associated with hostname")
    orig = socket.getaddrinfo
    socket.getaddrinfo = gone
    try:
        with pytest.raises(ValueError, match="could not be found"):
            fetch.check_url("https://recipes.example.invalid/x")
    finally:
        socket.getaddrinfo = orig


def test_cover_candidates_largest_first():
    html = '<meta property="og:image" content="https://a.com/og.jpg">'
    ld = {"image": ["https://a.com/p-225x225.jpg", "https://a.com/p-500x500.jpg", "https://a.com/p.jpg"]}
    urls = [u for u, _ in fetch._image_candidates(html, ld, "https://a.com/x")]
    assert urls == ["https://a.com/p.jpg", "https://a.com/og.jpg", "https://a.com/p-500x500.jpg", "https://a.com/p-225x225.jpg"]


def test_cover_candidates_use_image_object_size():
    ld = {"image": [{"url": "https://a.com/s.webp", "width": "300", "height": "300"}, {"url": "https://a.com/b.webp", "width": "1280", "height": "960"}]}
    assert fetch._image_candidates("", ld, "https://a.com/")[0][0] == "https://a.com/b.webp"


def test_save_cover_skips_blocked_and_html(tmp_path, monkeypatch):
    seen = []

    def fake_get(u, limit=0):
        seen.append(u)
        if "blocked" in u:
            raise RuntimeError("site refused the download (HTTP 403)")
        if "page" in u:
            return b"<html>" + b"x" * 5000
        return JPEG
    monkeypatch.setattr(fetch, "_get", fake_get)
    monkeypatch.setattr(fetch, "check_url", lambda u: u)
    ld = {"image": ["https://a.com/blocked.jpg", "https://a.com/page.jpg", "https://a.com/ok.jpg"]}
    assert fetch.save_cover(tmp_path, "https://a.com/r", "", ld) == (None, None)
    assert (tmp_path / "thumbnail.jpg").read_bytes() == JPEG
    assert len(seen) == 3


def test_save_cover_keeps_remote_url_when_all_fail(tmp_path, monkeypatch):
    def refuse(u, limit=0):
        raise RuntimeError("site refused the download (HTTP 403)")
    monkeypatch.setattr(fetch, "_get", refuse)
    monkeypatch.setattr(fetch, "check_url", lambda u: u)
    ld = {"image": [{"url": "https://cdn.example/big.webp", "width": "1280", "height": "1280"}]}
    url, err = fetch.save_cover(tmp_path, "https://a.com/r", "", ld)
    assert url == "https://cdn.example/big.webp" and "403" in err
    assert not (tmp_path / "thumbnail.jpg").exists()


TASTY = """<html><body><div class="tasty-recipes-instructions"><ol>
<li>Mix sauce ingredients: In a bowl, whisk the honey and soy.</li>
<li>Pour it over the chicken.</li>
<li>Pressure cook: Close the lid and cook 5 minutes.</li></ol></div></body></html>"""


def test_step_labels_put_back():
    steps = ["In a bowl, whisk the honey and soy.", "Pour it over the chicken.", "Close the lid and cook 5 minutes."]
    out, extras = P.label_steps(steps, TASTY)
    assert out[0].startswith("Mix sauce ingredients: In a bowl")
    assert out[1] == steps[1]
    assert out[2].startswith("Pressure cook: Close")
    assert extras == []


def test_step_labels_collect_unmatched_tip():
    html = TASTY.replace("</ol>", "<li>Do not use brown rice.</li></ol>")
    _, extras = P.label_steps(["In a bowl, whisk the honey and soy.", "Pour it over the chicken.", "Close the lid and cook 5 minutes."], html)
    assert extras == ["Do not use brown rice."]


def test_equipment_line_moves_to_notes(tmp_path):
    ld = {"name": "T", "recipeIngredient": ["1 cup rice", "equipment: Instant Pot, trivet"], "recipeInstructions": [{"@type": "HowToStep", "text": "Cook."}]}
    (tmp_path / "jsonld.json").write_text(json.dumps(ld), encoding="utf-8")
    r = P.from_jsonld(tmp_path)
    assert r["ingredients"] == ["1 cup rice"]
    assert r["page_extras"] == ["Equipment: Instant Pot, trivet"]


CHEF_NOTES = """<html><body><section class="o-ChefNotes"><header><h3>Cook’s Note</h3></header>
<p>Settings may vary on your Instant Pot.</p></section></body></html>"""


def test_food_network_cook_note():
    assert P.extract_notes(CHEF_NOTES) == ["Settings may vary on your Instant Pot."]


def test_note_reference_detected():
    assert P.NOTE_REF.search("melt the butter (see Cook's Note). Add the ginger")
    assert not P.NOTE_REF.search("Stir and serve.")


def test_redirect_to_another_recipe_detected():
    assert fetch.redirected_elsewhere("http://www.eatingwell.com/recipe/252015/minted-peas-rice-with-feta/",
                                      "https://www.eatingwell.com/recipe/272721/jerk-chicken-with-coconut-rice-peas/")
    assert fetch.redirected_elsewhere("https://a.com/recipes/chicken-biryani/", "https://a.com/")


def test_harmless_redirects_not_flagged():
    assert not fetch.redirected_elsewhere("https://a.com/pasta-caramelized-sweet-potatoes-kale/", "https://a.com/pasta-with-caramelized-sweet-potatoes-and-kale/")
    assert not fetch.redirected_elsewhere("https://a.com/chicken-biryani-instant-pot/", "https://a.com/instant-pot-chicken-biryani/")
    assert not fetch.redirected_elsewhere("http://a.com/x/same-slug/", "https://www.a.com/x/same-slug")


def test_not_found_message():
    assert "not found" in fetch._http_error(404) and "dead" in fetch._http_error(404)
    assert "refused" in fetch._http_error(403) and "HTTP 403" in fetch._http_error(403)


def test_page_title_entities_unescaped(tmp_path, monkeypatch):
    html = '<html><head><meta property="og:title" content="Jerk Chicken &amp; Peas"></head><body></body></html>'
    monkeypatch.setattr(fetch, "DATA", tmp_path)
    monkeypatch.setattr(fetch, "_open", lambda u, limit=0: (html.encode(), u))
    monkeypatch.setattr(fetch, "_get", lambda u, limit=0: (_ for _ in ()).throw(RuntimeError("no")))
    monkeypatch.setattr(fetch, "check_url", lambda u: u)
    d = fetch.fetch_page("https://a.com/x")
    assert "amp" not in d.name and json.loads((d / "fetch.json").read_text())["page_title"] == "Jerk Chicken & Peas"


def test_active_time_read_from_page():
    html = "<html><body><p>Total: 1 hr 45 min (includes resting time) Active: 35 min Yield: 4</p><style>.a:Active:hover{}</style></body></html>"
    assert P.active_minutes(html) == 35
    assert P.active_minutes("<html><body>Active: 1 hr 5 min</body></html>") == 65
    assert P.active_minutes("<html><body>no times</body></html>") is None
