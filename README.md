# RIRAONE backend

FastAPI service behind the WhatsApp ordering bot and the seller dashboard.

- **WhatsApp webhook** (`/webhook`): first message gets a language menu (19 languages), then a welcome
  with a "Shop now" button. The AI (Gemini by default) only sorts messages: wants to buy -> shop link with
  the cart pre-filled; order status -> answered from the order data; anything else -> the seller.
- **Seller alerts** (`seller_alerts.py`): questions go to the seller's own WhatsApp (`alert_phone`); the
  seller swipe-replies and the answer reaches the customer (`#bot` hands the chat back). Re-alert after
  5 min, "seller is busy" to the customer after 15 min.
- **Shop** (`/shop/{business_id}`): photos, sizes, nutrition facts, cart and checkout on one page, in the
  customer's language (`locales/*.json`). Checkout places the order and opens the pay page.
- **Dashboard API**: products, orders, inbox (`/conversations`), seller settings (`/business`), login.
- **Razorpay webhook** (`/payments/webhook/razorpay/{business_id}`): marks orders paid, deducts stock,
  sends the customer a confirmation with their invoice link.
- **Invoices** (`/invoices/{order_id}?t=...`): printable HTML at an unguessable public link.

## Environment variables

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | Yes in production | Postgres URL. Without it a local SQLite file is used - on Render that file is wiped on every deploy. |
| `AI_PROVIDER` | No | `gemini` (default, free tier) or `anthropic` (Claude, paid). |
| `GEMINI_API_KEY` | If gemini | Free key from aistudio.google.com. Without the active provider's key every chat is handed to the seller. |
| `GEMINI_MODEL` | No | Defaults to `gemini-flash-lite-latest` (free tier). With Gemini billing on, set `gemini-flash-latest`. |
| `GEMINI_FALLBACK_MODEL` | No | Tried when the main model is busy (503/429). Defaults to `gemini-flash-latest`; with billing on, set `gemini-flash-lite-latest`. |
| `CANCEL_UNPAID_AFTER_HOURS` | No | Unpaid online orders are cancelled after this long. Default `24`. |
| `ANTHROPIC_API_KEY` | If anthropic | Claude API key (console.anthropic.com). |
| `CLAUDE_MODEL` | No | Defaults to `claude-opus-5-5`; `claude-haiku-5-5` is far cheaper. |
| `SECRET_KEY` | Yes | Long random string. Encrypts sellers' WhatsApp/Razorpay secrets and signs logins and invoice links. **Never change it** once sellers have connected accounts - their saved keys become unreadable. |
| `WHATSAPP_VERIFY_TOKEN` | Yes | The verify token entered in Meta's webhook settings. |
| `WHATSAPP_APP_SECRET` | Recommended | Meta app secret (App settings → Basic). Webhook calls without a valid `X-Hub-Signature-256` are rejected. Comma-separate several if sellers use different Meta apps. Unset = not checked. |
| `DASHBOARD_PASSWORD` | Recommended | Password for the original business (id 1). Until set, business 1's dashboard API is open to anyone. |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TOKEN` | For business 1 | The original business's WhatsApp number, used when its Settings page is empty. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | For business 1 | Same fallback for Razorpay. Other sellers enter their own keys in Settings. |
| `SIGNUP_CODE` | Optional | Invite code: sellers who type it are approved at once. Without it, sign-ups wait for the founder's approval. |
| `OPEN_SIGNUP` | Optional | `0` = sign-up only with the invite code. Default: open (needs approval). Sign-up always needs `SECRET_KEY`. |
| `TRIAL_DAYS` | Optional | Free trial for new sellers, default `14`. |
| `BILLING_GRACE_DAYS` | Optional | Days after the due date before an unpaid shop pauses, default `7`. |
| `SELLER_DASHBOARD_URL` | Optional | Linked in billing messages, default `https://reone-dashboard.vercel.app`. |
| `FOUNDER_ALERT_PHONE` | Optional | Founder's WhatsApp for alerts, if not set in the founder console. |
| `GEO_LOOKUP` | Optional | `0` turns off IP → country/city lookups (ipwho.is, ip-api.com) for sign-ins and visits. |
| `PUBLIC_BASE_URL` | Optional | Public URL of this backend, used in invoice links. Default `https://reone-backend.onrender.com`. |
| `WA_TEMPLATE_ORDER_UPDATE`, `WA_TEMPLATE_ORDER_UPDATE_LANG` | Optional | Approved template for order updates sent more than 24h after the customer's last message. Default `order_update` / `en` - see `WHATSAPP_TEMPLATES.md`. |

Generate a `SECRET_KEY` with:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Running locally

```
pip install -r requirements.txt
uvicorn main:app --reload
```

Settings are read from a `.env` file in this folder. New database columns are added automatically at
startup (`migrate.py`), so an existing database upgrades in place.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests
```

Tests use a throwaway SQLite database and fake WhatsApp/Claude clients - they never send real
messages or spend API credits.
