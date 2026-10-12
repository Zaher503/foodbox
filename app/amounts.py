"""Fill missing ingredient amounts from phrases in a caption, e.g. "Half a teaspoon salt and pepper, 1 teaspoon cumin and paprika"."""
import re

AMT_RE = re.compile(
    r"^(?P<n>half a|half|a quarter|quarter|one|two|three|four|five|\d+(?:\.\d+)?(?:\s*/\s*\d+)?(?:\s*(?:-|to)\s*\d+)?|[¼-¾⅐-⅞])\s*"
    r"(?P<u>teaspoons?|tsps?|tablespoons?|tbsps?|cups?|ounces?|oz|pounds?|lbs?|grams?|g|ml|cloves?)\b\.?\s+(?P<rest>.+)$", re.I)
AMT_WORD = {"half a": "1/2", "half": "1/2", "a quarter": "1/4", "quarter": "1/4", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5"}
UNIT_SHORT = {"teaspoon": "tsp", "teaspoons": "tsp", "tsps": "tsp", "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsps": "tbsp",
              "ounce": "oz", "ounces": "oz", "pound": "lb", "pounds": "lb", "lbs": "lb", "gram": "g", "grams": "g"}
SPLIT_SEG = re.compile(r"[,;\n]|\s\d{1,2}\.\s|(?<=[a-z])\.\s")
SPLIT_AND = re.compile(r"\s+(?:and|&)\s+", re.I)
QTY_START = re.compile(r"^(\d|[¼-¾⅐-⅞]|a pinch|a dash|a handful)", re.I)


def caption_amounts(caption):
    """{item: "1/2 tsp"}; one amount can cover several items joined by "and"."""
    out = {}
    text = re.sub(r"#\S+", " ", caption or "")
    for seg in SPLIT_SEG.split(text):
        m = AMT_RE.match(seg.strip(" -•"))
        if not m:
            continue
        n = m["n"].lower()
        n = AMT_WORD.get(n, re.sub(r"\s+", "", n) if "/" in n else n)
        u = m["u"].lower()
        amt = f"{n} {UNIT_SHORT.get(u, u)}"
        for i in SPLIT_AND.split(m["rest"]):
            i = i.strip(" .").lower()
            if i and len(i.split()) <= 3:
                out.setdefault(i, amt)
    return out


def fill_caption_amounts(r, caption):
    """Give amount-less ingredients the amount the caption states for them. Lines that already have a number are left alone."""
    amts = caption_amounts(caption)
    if not amts:
        return r
    out = []
    for line in r.get("ingredients") or []:
        if re.search(r"\d", line) or QTY_START.match(line):
            out.append(line)
            continue
        found = {amts.get(x.strip().lower()) for x in SPLIT_AND.split(line)}
        out.append(f"{found.pop()} {line}" if len(found) == 1 and None not in found else line)
    r["ingredients"] = out
    return r
