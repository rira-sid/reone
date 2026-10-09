# RIRAONE backend

FastAPI service behind the WhatsApp ordering bot and the seller dashboard.

- **WhatsApp webhook** (`/webhook`): incoming messages are routed to the seller whose number they
  were sent to; Claude reads the order, collects name + address, replies in the customer's language,
  and turns a confirmed cart into an order with a Razorpay payment link.
- **Dashboard API**: products, orders, inbox (`/conversations`), seller settings (`/business`), login.
- **Razorpay webhook** (`/payments/webhook/razorpay/{business_id}`): marks orders paid, deducts stock,
  sends the customer a confirmation with their invoice link.
- **Invoices** (`/invoices/{order_id}?t=...`): printable HTML at an unguessable public link.

## Environment variables

| Variable | Required | What it does |
|---|---|---|
| `DATABASE_URL` | Yes in production | Postgres URL. Without it a local SQLite file is used - on Render that file is wiped on every deploy. |
| `ANTHROPIC_API_KEY` | Yes | Claude API key (console.anthropic.com). Without it every chat is handed to the seller. |
| `SECRET_KEY` | Yes | Long random string. Encrypts sellers' WhatsApp/Razorpay secrets and signs logins and invoice links. **Never change it** once sellers have connected accounts - their saved keys become unreadable. |
| `WHATSAPP_VERIFY_TOKEN` | Yes | The verify token entered in Meta's webhook settings. |
| `DASHBOARD_PASSWORD` | Recommended | Password for the original business (id 1). Until set, business 1's dashboard API is open to anyone. |
| `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TOKEN` | For business 1 | The original business's WhatsApp number, used when its Settings page is empty. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` | For business 1 | Same fallback for Razorpay. Other sellers enter their own keys in Settings. |
| `SIGNUP_CODE` | For new sellers | Invite code new sellers type to create an account. Sign-up is closed unless this and `SECRET_KEY` are set. |
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
