import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from tags import appliance_tags, cuisine_tag, make_tags, protein_tags  # noqa: E402


def ask_none(_):
    return {"cuisine": "none"}


def test_protein_ignores_broth_and_fish_sauce():
    r = {"ingredients": ["2 cups chicken broth", "1 tbsp fish sauce", "1 lb mushrooms"]}
    assert protein_tags(r) == ["vegetarian"]


def test_protein_finds_meats():
    r = {"ingredients": ["1 lb ground beef", "4 slices bacon", "1 cup beef broth"]}
    assert protein_tags(r) == ["beef", "pork"]


def test_no_ingredients_no_tags():
    assert make_tags({"ingredients": []}, ask_none) == []


def test_appliance_from_title_and_steps():
    r = {"title": "Instant Pot Chili", "steps": ["Set to pressure cook.", "Serve."]}
    assert appliance_tags(r) == ["instant pot"]


def test_cuisine_only_from_the_list():
    assert cuisine_tag({}, lambda _: {"cuisine": "Italian"}) == ["italian"]
    assert cuisine_tag({}, lambda _: {"cuisine": "martian"}) == []
    assert cuisine_tag({}, lambda _: 1 / 0) == []


def test_make_tags_order():
    r = {"title": "Slow Cooker Tacos", "ingredients": ["1 lb chicken thighs"], "steps": ["Add to the slow cooker."]}
    assert make_tags(r, lambda _: {"cuisine": "mexican"}) == ["chicken", "mexican", "slow cooker"]
