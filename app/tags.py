"""Recipe tags from a small fixed vocabulary: protein, appliance, cuisine. Nothing else becomes a tag."""
import re

CUISINES = ["mexican", "italian", "american", "thai", "indian", "korean", "chinese", "japanese",
            "mediterranean", "greek", "french", "middle eastern"]

# Appliance tag -> words in the title or directions that show the recipe uses it
APPLIANCES = {
    "instant pot": r"instant pot|pressure cook|multi-?cooker",
    "slow cooker": r"slow cooker|crock-?pot|slow cook",
    "air fryer": r"air fr(y|i)",
    "rice cooker": r"rice cooker",
    "blender": r"blender|\bblend\b",
    "grill": r"\bgrill(ed|ing)?\b|\bbbq\b|barbecue",
    "dutch oven": r"dutch oven",
    "sheet pan": r"sheet pan|baking sheet|sheet-pan",
}

# Protein tag -> ingredient words; checked after stock, broth and sauces are removed
PROTEINS = {
    "beef": r"beef|steak|brisket|sirloin|chuck|ribeye|short ribs?|meatballs?",
    "chicken": r"chicken|drumsticks?|wings?",
    "pork": r"pork|bacon|sausage|ham\b|prosciutto|pancetta|chorizo|pepperoni",
    "turkey": r"turkey",
    "lamb": r"lamb|mutton",
    "seafood": r"salmon|shrimp|prawns?|tuna|cod\b|tilapia|halibut|crab|lobster|scallops?|mussels|clams|fish\b|anchov",
}
NOT_MEAT = re.compile(r"\b(beef|chicken|pork|vegetable|veggie|turkey|fish|seafood|bone|mushroom)\s+(broth|stock|bouillon|base|consomm\w*)\b"
                      r"|\b(fish|oyster)\s+sauce\b|\bchicken[- ]flavou?red\b|\bbeef[- ]flavou?red\b", re.I)

CUISINE_PROMPT = """Which cuisine is this recipe, if it is obvious? Choose ONE from: {opts}.
Answer "none" unless the dish clearly belongs to one of them (tacos = mexican, pad thai = thai, lasagna = italian, burgers = american).
Return JSON {{"cuisine": "..."}}
Title: {title}
Ingredients: {ing}
"""


def appliance_tags(r):
    text = " ".join([r.get("title") or "", *(r.get("steps") or [])]).lower()
    return [t for t, pat in APPLIANCES.items() if re.search(pat, text)]


def protein_tags(r):
    """Meats found in the ingredients; 'vegetarian' when there are ingredients and none is meat or fish."""
    ings = [i if isinstance(i, str) else str(i.get("text") or i.get("item") or i) for i in r.get("ingredients") or []]
    if not ings:
        return []
    text = NOT_MEAT.sub(" ", " ; ".join(ings)).lower()
    found = [t for t, pat in PROTEINS.items() if re.search(rf"\b(?:{pat})", text)]
    return found or ["vegetarian"]


def cuisine_tag(r, ask):
    """ask(prompt) -> dict, supplied by the caller so this module stays free of model code."""
    try:
        j = ask(CUISINE_PROMPT.format(opts=", ".join(CUISINES), title=r.get("title"), ing=(r.get("ingredients") or [])[:20]))
    except Exception:
        return []
    c = str(j.get("cuisine") or "").lower().strip()
    return [c] if c in CUISINES else []


def make_tags(r, ask):
    if not r.get("ingredients"):
        return []
    out = []
    for t in [*protein_tags(r), *cuisine_tag(r, ask), *appliance_tags(r)]:
        if t not in out:
            out.append(t)
    return out
