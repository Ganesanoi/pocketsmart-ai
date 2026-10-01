import io
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_module
import gemini_utils
import products


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app, follow_redirects=False) as c:
        yield c


@pytest.fixture(scope="module")
def authed(client):
    r = client.post("/register", data={"username": "tester", "email": "t@example.com",
                                       "password": "password123", "confirm_password": "password123"})
    assert r.status_code == 303
    r = client.post("/login", data={"username": "tester", "password": "password123"})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    return client


def test_public_pages(client):
    for path in ("/", "/login", "/register", "/health"):
        assert client.get(path).status_code == 200


def test_protected_redirects_when_logged_out():
    with TestClient(app_module.app, follow_redirects=False) as c:
        r = c.get("/dashboard")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert c.get("/session-info", headers={"accept": "application/json"}).status_code == 401


def test_register_validation(client, authed):
    r = client.post("/register", data={"username": "x", "email": "bad", "password": "short", "confirm_password": "short"})
    assert r.status_code == 400
    r = client.post("/register", data={"username": "tester", "email": "t@example.com",
                                       "password": "password123", "confirm_password": "password123"})
    assert r.status_code == 400 and "taken" in r.text


def test_bad_login():
    with TestClient(app_module.app) as c:
        assert c.post("/login", data={"username": "tester", "password": "wrong"}).status_code == 401


def test_session_and_token(authed):
    assert authed.get("/session-info").json()["username"] == "tester"
    assert "activity" in authed.get("/session-data").json()
    with TestClient(app_module.app) as c:
        tok = c.post("/token", data={"username": "tester", "password": "password123"}).json()["access_token"]
        assert c.get("/session-info", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        assert c.get("/session-info", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_home_fallback_within_budget(authed):
    r = authed.post("/generate-home", data={"budget": "40000", "style": "Modern", "rooms": ["Living Room"],
                                            "qty_lights": "4", "qty_ceiling_fans": "2", "qty_dining_tables": "1"})
    assert r.status_code == 200 and "recommendations" in r.text and "built-in catalog" in r.text


def test_home_validation(authed):
    assert authed.post("/generate-home", data={"budget": "10"}).status_code == 400
    assert authed.post("/generate-home", data={"budget": "5000"}).status_code == 400  # no quantities


def test_party_and_validation(authed):
    r = authed.post("/generate-party", data={"budget": "60000", "guests": "40", "event_type": "birthday", "need_stay": "1"})
    assert r.status_code == 200 and "Suggested split" in r.text
    assert authed.post("/generate-party", data={"budget": "100", "guests": "40"}).status_code == 400
    assert authed.post("/generate-party", data={"budget": "60000", "guests": "0"}).status_code == 400


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(buf, "PNG")
    return buf.getvalue()


def test_jewelry_with_and_without_image(authed):
    r = authed.post("/generate-jewelry", data={"budget": "5000", "occasion": "wedding", "style": "traditional"})
    assert r.status_code == 200
    r = authed.post("/generate-jewelry", data={"budget": "5000", "occasion": "party", "style": "modern"},
                    files={"outfit": ("dress.png", _png(), "image/png")})
    assert r.status_code == 200 and "uploads/" in r.text
    r = authed.post("/generate-jewelry", data={"budget": "5000"},
                    files={"outfit": ("evil.png", b"<script>alert(1)</script>", "image/png")})
    assert r.status_code == 400


def test_history_pages(authed):
    assert "Recommendation history" in authed.get("/history").text
    assert authed.get("/recommendations-details/1").status_code == 200
    assert authed.get("/recommendations-details/9999").status_code == 404
    assert authed.get("/dashboard").status_code == 200


def test_history_is_private(authed):
    authed.post("/logout")
    with TestClient(app_module.app, follow_redirects=False) as c:
        c.post("/register", data={"username": "other", "email": "o@example.com", "password": "password123", "confirm_password": "password123"})
        c.post("/login", data={"username": "other", "password": "password123"})
        assert c.get("/recommendations-details/1").status_code == 404


# ---------------------------------------------------------- Gemini layer
def test_normalize_enforces_budget():
    raw = {"summary": "x", "items": [
        {"category": "A", "name": "cheap", "platform": "Amazon", "estimated_price": 100, "quantity": 2},
        {"category": "B", "name": "too pricey", "platform": "Flipkart", "estimated_price": 9000, "quantity": 1},
        {"category": "C", "name": "bad", "estimated_price": "abc"}]}
    out = gemini_utils.normalize(raw, 1000)
    assert [i["name"] for i in out["items"]] == ["cheap"] and out["total"] == 200 and out["remaining"] == 800


def test_normalize_unknown_platform_and_empty():
    out = gemini_utils.normalize({"items": [{"name": "x", "platform": "Evil", "estimated_price": 5}]}, 100)
    assert out["items"][0]["platform"] == "Other"
    assert gemini_utils.normalize({"items": []}, 100) == {}


def test_gemini_success_path(monkeypatch):
    monkeypatch.setattr(gemini_utils, "call_gemini", lambda prompt, image=None: {
        "summary": "ok", "items": [{"category": "Lights", "name": "Lamp", "platform": "IKEA", "estimated_price": 500, "quantity": 2}]})
    res = gemini_utils.home_recommendations(5000, "Modern", ["Bedroom"], {"lights": 2})
    assert res["source"] == "gemini" and res["total"] == 1000


def test_gemini_error_falls_back(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("quota")
    monkeypatch.setattr(gemini_utils, "call_gemini", boom)
    assert gemini_utils.jewelry_recommendations(3000, "party", "modern", "any")["source"] == "fallback"


@pytest.mark.parametrize("budget", [1000, 5000, 25000, 100000, 500000])
def test_fallbacks_never_exceed_budget(budget):
    h = products.fallback_home(budget, {"lights": 3, "ceiling_fans": 2, "sofas": 1, "beds": 1})
    p = products.fallback_party(budget, 30, "wedding", True)
    j = products.fallback_jewelry(budget, "wedding", "traditional")
    for r in (h, p, j):
        assert sum(i["estimated_price"] * i["quantity"] for i in r["items"]) <= budget
