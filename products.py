"""Product catalog (mock data), budget allocation and rule-based fallbacks.

The document's Activity 3.4 asks for "mock API calls or simulated scraping".
Prices are approximate INR figures for demo purposes, and links are search URLs
built from the product name - no scraping is done.
"""
from urllib.parse import quote_plus

PLATFORMS = {
    "Amazon": "https://www.amazon.in/s?k={q}",
    "Flipkart": "https://www.flipkart.com/search?q={q}",
    "IKEA": "https://www.ikea.com/in/en/search/?q={q}",
    "Swiggy": "https://www.swiggy.com/search?query={q}",
    "Zomato": "https://www.zomato.com/search?q={q}",
    "OYO": "https://www.oyorooms.com/search?location={q}",
    "Other": "https://www.google.com/search?q={q}",
}


def platform_link(platform: str, query: str) -> str:
    return PLATFORMS.get(platform, PLATFORMS["Other"]).format(q=quote_plus(query))


def item(category, name, platform, price, qty=1, reason="", query=None):
    return {
        "category": category, "name": name, "platform": platform,
        "estimated_price": float(price), "quantity": int(qty), "reason": reason,
        "search_query": query or name, "url": platform_link(platform, query or name),
    }


# --------------------------------------------------------------------- HOME
HOME_CATALOG = {
    "lights": [("LED batten light 20W", "Amazon", 450), ("Pendant lamp, rattan", "IKEA", 1499), ("Designer chandelier", "Flipkart", 3999)],
    "ceiling_fans": [("BLDC energy-saving ceiling fan", "Amazon", 2999), ("Decorative ceiling fan 1200mm", "Flipkart", 3499), ("Premium remote-control fan", "Amazon", 5999)],
    "dining_tables": [("4-seater folding dining set", "Amazon", 6999), ("Sheesham 4-seater dining table", "Flipkart", 11999), ("Ingatorp extendable table", "IKEA", 18990)],
    "beds": [("Engineered wood queen bed", "Flipkart", 9999), ("Malm-style queen bed frame", "IKEA", 16990), ("Solid wood king bed with storage", "Amazon", 24999)],
    "sofas": [("3-seater fabric sofa", "Flipkart", 12999), ("Friheten-style corner sofa bed", "IKEA", 29990), ("Leatherette L-shape sofa", "Amazon", 34999)],
    "wall_art": [("Framed canvas print set of 3", "Amazon", 799), ("Abstract art frame", "IKEA", 1299), ("Large hand-painted canvas", "Flipkart", 3499)],
    "curtains": [("Polyester door curtains (pair)", "Amazon", 599), ("Blackout curtains (pair)", "Flipkart", 1499), ("Linen-blend curtains (pair)", "IKEA", 2499)],
}
HOME_LABELS = {
    "lights": "Lights", "ceiling_fans": "Ceiling fans", "dining_tables": "Dining tables",
    "beds": "Beds", "sofas": "Sofas", "wall_art": "Wall art", "curtains": "Curtains",
}


def fallback_home(budget, quantities):
    wanted = {k: q for k, q in quantities.items() if q > 0 and k in HOME_CATALOG}
    if not wanted:
        wanted = {"lights": 4, "ceiling_fans": 2, "curtains": 2}
    avg = {k: sum(p for _, _, p in HOME_CATALOG[k]) / 3 for k in wanted}
    total_weight = sum(avg[k] * q for k, q in wanted.items())
    items, spent = [], 0.0
    for k, q in wanted.items():
        share = budget * (avg[k] * q) / total_weight
        cap = share / q
        options = sorted(HOME_CATALOG[k], key=lambda o: o[2])
        choice = max((o for o in options if o[2] <= cap), key=lambda o: o[2], default=None)
        if choice is None:
            choice = options[0]
            if choice[2] * q > budget - spent:
                continue
        name, platform, price = choice
        spent += price * q
        items.append(item(HOME_LABELS[k], name, platform, price, q,
                          "Best fit for this item's share of your budget."))
    return {"summary": "Budget-balanced picks from our built-in catalog.", "items": items,
            "tips": ["Compare prices across platforms before buying.", "Buy large items during sale seasons."]}


# -------------------------------------------------------------------- PARTY
PARTY_SPLIT = {
    "birthday": {"catering": .40, "decoration": .25, "entertainment": .20, "venue": .15},
    "corporate": {"catering": .45, "decoration": .15, "entertainment": .15, "venue": .25},
    "wedding": {"catering": .45, "decoration": .25, "entertainment": .10, "venue": .20},
    "anniversary": {"catering": .40, "decoration": .25, "entertainment": .15, "venue": .20},
    "other": {"catering": .40, "decoration": .25, "entertainment": .20, "venue": .15},
}


def party_allocation(budget, event_type):
    split = PARTY_SPLIT.get(event_type, PARTY_SPLIT["other"])
    return {k: round(budget * v) for k, v in split.items()}


PARTY_CATALOG = {
    # catering: price PER HEAD
    "catering": [("Veg buffet (per head)", "Zomato", 250), ("Mixed party platters (per head)", "Swiggy", 400), ("Premium multi-cuisine catering (per head)", "Zomato", 700)],
    "decoration": [("Balloon & banner decoration kit", "Amazon", 1200), ("Theme decoration package with lights", "Flipkart", 3500), ("Floral backdrop + fairy-light setup", "Amazon", 8000)],
    "entertainment": [("Bluetooth party speaker + DJ lights", "Amazon", 2500), ("Games & activity host (2 hrs)", "Other", 5000), ("Live DJ / band booking", "Other", 15000)],
    "venue": [("Home / terrace setup", "Other", 0), ("Small party hall booking (4 hrs)", "OYO", 6000), ("Banquet hall booking", "OYO", 20000)],
}


def fallback_party(budget, guests, event_type, need_stay=False):
    alloc = party_allocation(budget, event_type)
    items = []
    per_head_cap = alloc["catering"] / guests
    opts = sorted(PARTY_CATALOG["catering"], key=lambda o: o[2])
    c = max((o for o in opts if o[2] <= per_head_cap), key=lambda o: o[2], default=None)
    if c:
        items.append(item("Catering", c[0], c[1], c[2] * guests, 1, f"{guests} guests x Rs.{c[2]:.0f} per head."))
    for cat in ("decoration", "entertainment", "venue"):
        opts = sorted(PARTY_CATALOG[cat], key=lambda o: o[2])
        c = max((o for o in opts if o[2] <= alloc[cat]), key=lambda o: o[2], default=None)
        if c:
            items.append(item(cat.capitalize(), c[0], c[1], c[2], 1, f"Fits the Rs.{alloc[cat]:,} {cat} allocation."))
    if need_stay:
        left = budget - sum(i["estimated_price"] * i["quantity"] for i in items)
        if left >= 1500:
            items.append(item("Accommodation", "Budget hotel room for out-of-town guests (1 night)", "OYO", 1500, 1,
                              "Optional stay for guests travelling in.", "hotel rooms"))
    return {"summary": f"Suggested {event_type} plan for {guests} guests.", "allocation": alloc, "items": items,
            "tips": ["Confirm final guest count 3 days ahead.", "Book caterers and venues early for weekends."]}


# ------------------------------------------------------------------ JEWELRY
# (name, platform, price, occasions, styles)
JEWELRY_CATALOG = [
    ("Oxidised silver jhumka earrings", "Amazon", 399, {"festival", "casual"}, {"traditional", "boho"}),
    ("Kundan choker necklace set", "Flipkart", 1499, {"wedding", "festival"}, {"traditional"}),
    ("Gold-plated temple necklace", "Amazon", 2499, {"wedding", "festival"}, {"traditional"}),
    ("American diamond stud earrings", "Flipkart", 599, {"party", "office", "wedding"}, {"modern", "minimal"}),
    ("Sterling silver minimalist pendant", "Amazon", 1299, {"office", "casual", "party"}, {"minimal", "modern"}),
    ("Pearl drop earrings", "Amazon", 799, {"office", "wedding", "party"}, {"classic", "minimal"}),
    ("Stackable rose-gold bracelet set", "Flipkart", 899, {"casual", "party"}, {"modern", "boho"}),
    ("Statement cocktail ring", "Amazon", 499, {"party"}, {"modern", "boho"}),
    ("Silk-thread bangles set of 12", "Flipkart", 349, {"festival", "wedding"}, {"traditional"}),
    ("Maang tikka with pearl drops", "Amazon", 699, {"wedding", "festival"}, {"traditional", "classic"}),
    ("Layered chain necklace", "Flipkart", 549, {"casual", "party"}, {"boho", "modern"}),
    ("18k gold-tone hoop earrings", "Amazon", 999, {"office", "casual", "party"}, {"classic", "modern"}),
]


def fallback_jewelry(budget, occasion, style):
    scored = []
    for name, plat, price, occs, styles in JEWELRY_CATALOG:
        if price > budget:
            continue
        score = (2 if occasion in occs else 0) + (1 if style in styles else 0)
        scored.append((score, price, name, plat))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    items, spent = [], 0.0
    for score, price, name, plat in scored:
        if len(items) == 4:
            break
        if score > 0 and spent + price <= budget:
            spent += price
            items.append(item("Jewelry", name, plat, price, 1, f"Suits a {occasion} look in a {style} style."))
    return {"summary": f"Pieces matched to a {occasion} occasion.", "items": items,
            "tips": ["Pick one statement piece and keep the rest subtle.", "Match metal tone (gold/silver) to your outfit."]}
