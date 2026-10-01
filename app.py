"""PocketSmart AI - FastAPI backend.  Run:  uvicorn app:app --reload"""
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from starlette.middleware.sessions import SessionMiddleware

import gemini_utils
import products

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pocketsmart")

BASE = Path(__file__).parent
DB_PATH = os.getenv("DB_PATH", str(BASE / "pocketsmart.db"))
UPLOAD_DIR = BASE / "static" / "uploads"
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
MAX_UPLOAD = 5 * 1024 * 1024
ALLOWED_IMAGES = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


# ---------------------------------------------------------------- database
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            email TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, category TEXT NOT NULL,
            budget REAL NOT NULL, inputs TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id));""")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$")
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(check, digest)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):  # "/startup" - initialise services when the server starts
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not os.getenv("GEMINI_API_KEY"):
        log.warning("GEMINI_API_KEY not set - recommendations will use the built-in fallback catalog.")
    yield


app = FastAPI(title="PocketSmart AI", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", max_age=86400)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:8000").split(","),
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# -------------------------------------------------------------------- auth
class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def not_authenticated(request: Request, exc: NotAuthenticated):
    if request.headers.get("authorization") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def create_token(user_id: int, username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_MINUTES)
    return jwt.encode({"sub": str(user_id), "name": username, "exp": exp}, SECRET_KEY, algorithm="HS256")


def get_current_user(request: Request):
    user_id = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            user_id = int(jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"])["sub"])
        except (jwt.PyJWTError, ValueError):
            raise NotAuthenticated()
    else:
        user_id = request.session.get("user_id")
    if not user_id:
        raise NotAuthenticated()
    with closing(db()) as conn:
        row = conn.execute("SELECT id, username, email FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        request.session.clear()
        raise NotAuthenticated()
    return dict(row)


def render(request, name, user=None, status=200, **ctx):
    return templates.TemplateResponse(request, name, {"user": user, **ctx}, status_code=status)


# ------------------------------------------------------------ public pages
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = None
    if request.session.get("user_id"):
        try:
            user = get_current_user(request)
        except NotAuthenticated:
            pass
    return render(request, "index.html", user)


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return render(request, "register.html")


@app.post("/register")
async def register(request: Request, username: str = Form(...), email: str = Form(...),
                   password: str = Form(...), confirm_password: str = Form(...)):
    username, email = username.strip(), email.strip()
    error = None
    if not (3 <= len(username) <= 30) or not username.replace("_", "").isalnum():
        error = "Username must be 3-30 letters, digits or underscores."
    elif "@" not in email or len(email) > 120:
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords do not match."
    if not error:
        try:
            with closing(db()) as conn, conn:
                conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES(?,?,?,?)",
                             (username, email, hash_password(password), now()))
        except sqlite3.IntegrityError:
            error = "That username is already taken."
    if error:
        return render(request, "register.html", status=400, error=error)
    return RedirectResponse("/login?registered=1", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, registered: int = 0):
    return render(request, "login.html", registered=bool(registered))


def _authenticate(username: str, password: str):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return row
    return None


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    row = _authenticate(username, password)
    if not row:
        return render(request, "login.html", status=401, error="Invalid username or password.")
    request.session.clear()
    request.session["user_id"] = row["id"]
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/token")
async def token(username: str = Form(...), password: str = Form(...)):
    row = _authenticate(username, password)
    if not row:
        raise HTTPException(401, "Invalid username or password")
    return {"access_token": create_token(row["id"], row["username"]), "token_type": "bearer"}


@app.get("/session-info")
async def session_info(user=Depends(get_current_user)):
    return {"user_id": user["id"], "username": user["username"], "logged_in": True}


@app.get("/session-data")
async def session_data(user=Depends(get_current_user)):
    with closing(db()) as conn:
        rows = conn.execute("SELECT category, COUNT(*) n, MAX(created_at) last FROM history WHERE user_id=? GROUP BY category",
                            (user["id"],)).fetchall()
    return {"user": user["username"], "activity": {r["category"]: {"count": r["n"], "last": r["last"]} for r in rows}}


# ---------------------------------------------------------- dashboard/history
def _history(user_id, limit=None):
    q = "SELECT id, category, budget, result, created_at FROM history WHERE user_id=? ORDER BY id DESC"
    with closing(db()) as conn:
        rows = conn.execute(q + (f" LIMIT {int(limit)}" if limit else ""), (user_id,)).fetchall()
    return [{"id": r["id"], "category": r["category"], "budget": r["budget"], "created_at": r["created_at"],
             "total": json.loads(r["result"]).get("total", 0)} for r in rows]


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user=Depends(get_current_user)):
    items = _history(user["id"])
    return render(request, "dashboard.html", user, recent=items[:5], count=len(items),
                  budget_total=sum(i["budget"] for i in items))


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request, user=Depends(get_current_user)):
    return render(request, "history.html", user, items=_history(user["id"]))


@app.get("/recommendations-details/{rec_id}", response_class=HTMLResponse)
async def recommendation_details(request: Request, rec_id: int, user=Depends(get_current_user)):
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM history WHERE id=? AND user_id=?", (rec_id, user["id"])).fetchone()
    if not row:
        raise HTTPException(404, "Recommendation not found")
    return render(request, "recommendations.html", user, category=row["category"], budget=row["budget"],
                  result=json.loads(row["result"]), inputs=json.loads(row["inputs"]), from_history=True)


def _save(user_id, category, budget, inputs, result) -> int:
    with closing(db()) as conn, conn:
        cur = conn.execute("INSERT INTO history(user_id,category,budget,inputs,result,created_at) VALUES(?,?,?,?,?,?)",
                           (user_id, category, budget, json.dumps(inputs), json.dumps(result), now()))
        return cur.lastrowid


def _show(request, user, category, budget, inputs, result):
    _save(user["id"], category, budget, inputs, result)
    return render(request, "recommendations.html", user, category=category, budget=budget,
                  result=result, inputs=inputs)


def _bad(request, user, template, message, **ctx):
    return render(request, template, user, status=400, error=message, **ctx)


# ------------------------------------------------------------------- home
@app.get("/home-planner", response_class=HTMLResponse)
async def home_planner(request: Request, user=Depends(get_current_user)):
    return render(request, "home_planner.html", user, labels=products.HOME_LABELS)


@app.post("/generate-home", response_class=HTMLResponse)
async def generate_home(request: Request, user=Depends(get_current_user)):
    form = await request.form()
    try:
        budget = float(form.get("budget", 0))
    except ValueError:
        budget = 0
    rooms = [r for r in form.getlist("rooms") if r in {"Living Room", "Kitchen", "Bedroom", "Dining Room", "Study"}]
    style = (form.get("style") or "Modern")[:40]
    qty = {}
    for key in products.HOME_LABELS:
        try:
            qty[key] = max(0, min(50, int(form.get(f"qty_{key}") or 0)))
        except ValueError:
            qty[key] = 0
    ctx = {"labels": products.HOME_LABELS}
    if budget < 1000 or budget > 10_000_000:
        return _bad(request, user, "home_planner.html", "Enter a budget between Rs.1,000 and Rs.1,00,00,000.", **ctx)
    if not any(qty.values()):
        return _bad(request, user, "home_planner.html", "Choose a quantity for at least one item.", **ctx)
    result = gemini_utils.home_recommendations(budget, style, rooms, qty)
    inputs = {"Style": style, "Rooms": ", ".join(rooms) or "-",
              "Items": ", ".join(f"{products.HOME_LABELS[k]} x{v}" for k, v in qty.items() if v)}
    return _show(request, user, "Home", budget, inputs, result)


# ------------------------------------------------------------------ party
@app.get("/party-planner", response_class=HTMLResponse)
async def party_planner(request: Request, user=Depends(get_current_user)):
    return render(request, "party_planner.html", user)


@app.post("/generate-party", response_class=HTMLResponse)
async def generate_party(request: Request, budget: float = Form(...), guests: int = Form(...),
                         event_type: str = Form("birthday"), venue: str = Form(""),
                         need_stay: str = Form(""), user=Depends(get_current_user)):
    event_type = event_type if event_type in products.PARTY_SPLIT else "other"
    venue = venue.strip()[:120]
    if not (2000 <= budget <= 10_000_000):
        return _bad(request, user, "party_planner.html", "Enter a budget between Rs.2,000 and Rs.1,00,00,000.")
    if not (1 <= guests <= 2000):
        return _bad(request, user, "party_planner.html", "Guest count must be between 1 and 2000.")
    stay = bool(need_stay)
    result = gemini_utils.party_recommendations(budget, guests, event_type, venue, stay)
    inputs = {"Event": event_type.title(), "Guests": guests, "Venue": venue or "-", "Guest accommodation": "Yes" if stay else "No"}
    return _show(request, user, "Party", budget, inputs, result)


# ---------------------------------------------------------------- jewelry
@app.get("/jewelry-planner", response_class=HTMLResponse)
async def jewelry_planner(request: Request, user=Depends(get_current_user)):
    return render(request, "jewelry_planner.html", user)


async def _read_image(upload: UploadFile | None):
    """Validate an optional outfit image. Returns (bytes, mime, saved_name) or None."""
    if not upload or not upload.filename:
        return None
    data = await upload.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ValueError("Image must be smaller than 5 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        fmt = img.format
    except Exception:
        raise ValueError("Please upload a valid JPG, PNG or WEBP image.")
    if fmt not in ALLOWED_IMAGES:
        raise ValueError("Please upload a valid JPG, PNG or WEBP image.")
    mime, ext = ALLOWED_IMAGES[fmt]
    name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return data, mime, name


@app.post("/generate-jewelry", response_class=HTMLResponse)
async def generate_jewelry(request: Request, budget: float = Form(...), occasion: str = Form("party"),
                           style: str = Form("modern"), metal: str = Form("any"),
                           outfit: UploadFile | None = File(None), user=Depends(get_current_user)):
    occasion = occasion if occasion in {"wedding", "party", "office", "festival", "casual"} else "party"
    style = style if style in {"traditional", "modern", "minimal", "classic", "boho"} else "modern"
    metal = metal if metal in {"any", "gold", "silver", "rose gold"} else "any"
    if not (500 <= budget <= 10_000_000):
        return _bad(request, user, "jewelry_planner.html", "Enter a budget between Rs.500 and Rs.1,00,00,000.")
    try:
        image = await _read_image(outfit)
    except ValueError as exc:
        return _bad(request, user, "jewelry_planner.html", str(exc))
    result = gemini_utils.jewelry_recommendations(budget, occasion, style, metal,
                                                  (image[0], image[1]) if image else None)
    inputs = {"Occasion": occasion.title(), "Style": style.title(), "Metal": metal.title(),
              "Outfit image": "Uploaded" if image else "None"}
    if image:
        result["image"] = image[2]
    return _show(request, user, "Jewelry", budget, inputs, result)


@app.get("/health")
async def health():
    return {"status": "ok", "gemini_configured": bool(os.getenv("GEMINI_API_KEY")), "model": gemini_utils.MODEL}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
