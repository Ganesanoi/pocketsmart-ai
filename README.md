# PocketSmart AI - Smart Budget & Recommendation Assistant

FastAPI + Google Gemini web app with three budget planners (Home, Party, Jewelry), user accounts,
and recommendation history.

## Run it
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then add your GEMINI_API_KEY (aistudio.google.com)
uvicorn app:app --reload                              # open http://127.0.0.1:8000
```
No API key? The app still works - it uses the built-in catalog in `products.py` as a fallback.

## Test it
```bash
python -m pytest tests -q
```

## Layout (matches the project document)
| Path | Purpose |
|---|---|
| `app.py` | FastAPI routes: auth, planners, history, session endpoints, startup |
| `gemini_utils.py` | Prompts, multimodal Gemini call, JSON validation, budget enforcement, fallback |
| `products.py` | Mock catalog (Amazon/Flipkart/IKEA/Swiggy/Zomato/OYO), budget split, rule-based fallback |
| `templates/` | dashboard, history, home/party/jewelry planners, recommendations, index, login, register |
| `static/styles.css`, `static/uploads/` | Styling and uploaded outfit images |

## Endpoints
`/register` `/login` `/logout` `/token` (JWT) `/session-info` `/session-data`
`/generate-home` `/generate-party` `/generate-jewelry` `/history` `/recommendations-details/{id}` `/health`

## How budget adherence works
Gemini is asked for JSON only. `normalize()` validates every item, maps unknown platforms to "Other",
builds search links itself (the model never supplies URLs), and drops lines until the total fits the budget.
If Gemini fails or returns nothing usable, `products.py` builds a plan that fits the budget.

## Before deploying
Set a strong `SECRET_KEY`, serve over HTTPS, set `CORS_ORIGINS`, and consider adding CSRF protection
and rate limiting. Product prices/links are demo data, not live marketplace feeds.
