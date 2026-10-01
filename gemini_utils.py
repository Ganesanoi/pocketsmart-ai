"""Gemini integration: prompt building, multimodal calls, validation and fallback."""
import json
import logging
import os
import re

from dotenv import load_dotenv

import products

load_dotenv()
log = logging.getLogger("pocketsmart.gemini")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_client = None


def get_client():
    """Return a Gemini client, or None when no API key is configured."""
    global _client
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=key)
    return _client


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    return json.loads(text)


def call_gemini(prompt: str, image: tuple | None = None) -> dict:
    """Send a text (+ optional image) prompt and return parsed JSON."""
    client = get_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not set")
    from google.genai import types
    parts = [prompt]
    if image:
        parts.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
    resp = client.models.generate_content(
        model=MODEL, contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
    )
    return _parse_json(resp.text)


def test_connection() -> str:
    """Activity 1.3 - simple connectivity check (text only)."""
    client = get_client()
    if client is None:
        return "GEMINI_API_KEY not set"
    return client.models.generate_content(model=MODEL, contents="Reply with the word OK.").text.strip()


# --------------------------------------------------------------- validation
SCHEMA = """Respond with ONLY valid JSON in this exact shape:
{"summary": "one or two sentences",
 "items": [{"category": "...", "name": "specific product/service", "platform": "one of: Amazon, Flipkart, IKEA, Swiggy, Zomato, OYO, Other",
            "estimated_price": <number, INR, price for ONE unit (or total for the line if quantity is 1)>,
            "quantity": <integer>, "reason": "short reason", "search_query": "text to search on the platform"}],
 "tips": ["short money-saving tip", "..."]}
Rules: all prices are in Indian Rupees. The sum of estimated_price x quantity across items MUST NOT exceed the budget.
Never invent URLs. Do not include any text outside the JSON."""


def normalize(result: dict, budget: float) -> dict:
    """Clean model output and enforce the budget. Returns {} when nothing usable remains."""
    clean = []
    for raw in (result or {}).get("items", []):
        try:
            price = float(raw["estimated_price"])
            qty = max(1, int(raw.get("quantity", 1)))
            name = str(raw["name"]).strip()
            if price < 0 or not name:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        platform = raw.get("platform") if raw.get("platform") in products.PLATFORMS else "Other"
        clean.append(products.item(str(raw.get("category", "General"))[:40], name[:120], platform, price, qty,
                                   str(raw.get("reason", ""))[:300], str(raw.get("search_query") or name)[:120]))
    # drop lowest-priority (last) lines until the plan fits the budget
    while clean and sum(i["estimated_price"] * i["quantity"] for i in clean) > budget:
        clean.pop()
    if not clean:
        return {}
    total = sum(i["estimated_price"] * i["quantity"] for i in clean)
    out = {"summary": str(result.get("summary", ""))[:500], "items": clean,
           "tips": [str(t)[:200] for t in result.get("tips", [])][:5]}
    if isinstance(result.get("allocation"), dict):
        out["allocation"] = result["allocation"]
    out["total"], out["remaining"] = round(total), round(budget - total)
    return out


def _finish(result: dict, budget: float, source: str) -> dict:
    total = sum(i["estimated_price"] * i["quantity"] for i in result["items"])
    result["total"], result["remaining"], result["source"] = round(total), round(budget - total), source
    return result


def _run(prompt, budget, fallback_fn, image=None):
    try:
        result = normalize(call_gemini(prompt, image), budget)
        if result:
            return result | {"source": "gemini"}
        log.warning("Gemini returned no usable items; using fallback")
    except Exception as exc:  # network, quota, bad JSON, missing key ...
        log.warning("Gemini unavailable (%s); using fallback", exc)
    return _finish(fallback_fn(), budget, "fallback")


# ---------------------------------------------------------------- planners
def home_recommendations(budget, style, rooms, quantities):
    wanted = {products.HOME_LABELS[k]: q for k, q in quantities.items() if q > 0 and k in products.HOME_LABELS}
    prompt = (
        f"You are PocketSmart AI, a budget-aware home interior assistant for Indian shoppers.\n"
        f"Total budget: Rs.{budget:,.0f}. Style: {style}. Rooms: {', '.join(rooms) or 'not specified'}.\n"
        f"Items needed (type: quantity): {wanted or 'suggest a sensible starter set'}.\n"
        f"Recommend one cost-effective option per item type from Amazon, Flipkart or IKEA, balancing function, style and price.\n\n{SCHEMA}")
    return _run(prompt, budget, lambda: products.fallback_home(budget, quantities))


def party_recommendations(budget, guests, event_type, venue, need_stay):
    alloc = products.party_allocation(budget, event_type)
    prompt = (
        f"You are PocketSmart AI, a party budget planner for India.\n"
        f"Event: {event_type}. Guests: {guests}. Venue details: {venue or 'not specified'}. Total budget: Rs.{budget:,.0f}.\n"
        f"Target allocation (INR): {json.dumps(alloc)}. Accommodation for guests wanted: {need_stay}.\n"
        f"Suggest catering (Swiggy/Zomato; price the line for ALL guests), decoration (Amazon/Flipkart), entertainment, "
        f"venue and optionally accommodation (OYO) that stay within each allocation. Include an \"allocation\" object in the JSON.\n\n{SCHEMA}")
    res = _run(prompt, budget, lambda: products.fallback_party(budget, guests, event_type, need_stay))
    res.setdefault("allocation", alloc)
    return res


def jewelry_recommendations(budget, occasion, style, metal, image=None):
    prompt = (
        f"You are PocketSmart AI, a jewelry stylist for Indian shoppers.\n"
        f"Budget: Rs.{budget:,.0f}. Occasion: {occasion}. Style: {style}. Preferred metal tone: {metal}.\n"
        + ("An outfit photo is attached: match colours and neckline to it and mention this in the reasons.\n" if image else "")
        + f"Suggest 3-5 pieces from Amazon or Flipkart that work together as a set.\n\n{SCHEMA}")
    return _run(prompt, budget, lambda: products.fallback_jewelry(budget, occasion, style), image)
