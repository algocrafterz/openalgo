# Signal Engine — Broker Setup & Auth Guide

How broker authentication works in the signal engine, and how to switch between brokers.

---

## How the broker is detected

The signal engine reads the active broker from your `.env` file using this priority:

| Source | Example | Notes |
|--------|---------|-------|
| `BROKER_NAME` (explicit) | `BROKER_NAME=flattrade` | Takes priority over REDIRECT_URL |
| `REDIRECT_URL` (auto-detect) | `REDIRECT_URL=http://127.0.0.1:5000/flattrade/callback` | Broker = first path segment |

**Recommended**: leave `BROKER_NAME` unset and let it auto-detect from `REDIRECT_URL`, which you already configure for OAuth callback. This way switching brokers requires changing only `REDIRECT_URL` and the broker credentials.

If neither is set, startup fails immediately with a clear error.

---

## Auth modes

### Programmatic (TOTP) — auto-login supported

Broker has `authenticate_with_totp` in its auth module. The scheduler generates a TOTP code and logs in directly — **no browser required**. Works unattended on a schedule.

| Broker | Status |
|--------|--------|
| **mstock** | Programmatic TOTP |
| **flattrade** | Programmatic TOTP (NorenAPI PiConnect) |
| angel, firstock, fivepaisa, … | Programmatic TOTP (check broker module) |

### OAuth-only — manual browser login required once per session

Broker requires an OAuth redirect flow. The scheduler cannot log in programmatically. You must log in via the OpenAlgo web UI first; the scheduler then reuses the stored token.

| Broker | Notes |
|--------|-------|
| zerodha | OAuth, token valid one session |
| upstox, groww, … | OAuth |

---

## Setting up mstock

```ini
# .env
REDIRECT_URL = 'http://127.0.0.1:5000/mstock/callback'
BROKER_API_KEY = MA6718246          # your mstock client code
BROKER_API_SECRET =                 # not required for mstock TOTP login
BROKER_PASSWORD = your_password
BROKER_TOTP_SECRET = BASE32SECRET   # from your authenticator app setup
```

`BROKER_NAME` is not needed — detected from `REDIRECT_URL`.

---

## Setting up flattrade

```ini
# .env
REDIRECT_URL = 'http://127.0.0.1:5000/flattrade/callback'
BROKER_API_KEY = FZ40074:::your_api_key   # CLIENT_ID:::API_KEY from flattrade developer account
BROKER_API_SECRET = your_api_secret       # API secret from flattrade developer account
BROKER_PASSWORD = your_password
BROKER_TOTP_SECRET = BASE32SECRET         # from your authenticator app setup
```

**BROKER_API_KEY format**: `CLIENT_ID:::API_KEY`
- `CLIENT_ID` = your Flattrade trading account ID (e.g. `FZ40074`)
- `API_KEY` = API key from the Flattrade developer portal

The scheduler uses a **headless OAuth flow** (no browser, no vendor registration required):
```
1. POST https://authapi.flattrade.in/auth/session
   → returns SID (session token)

2. POST https://authapi.flattrade.in/ftauth
   Body: {UserName, Password: SHA256(pwd), PAN_DOB: TOTP, APIKey, Sid}
   → returns RedirectURL containing the OAuth code

3. POST https://authapi.flattrade.in/trade/apitoken
   Body: {api_key, request_code, api_secret: SHA256(api_key+code+api_secret)}
   → returns auth token
```

---

## Switching between brokers

Both mstock and flattrade support programmatic TOTP login, so switching is seamless:

1. Update `.env` with the new broker's credentials and `REDIRECT_URL`
2. Restart the signal engine

The scheduler will call `authenticate_with_totp` on the new broker module, get a fresh token, and store it. The old broker's token is replaced automatically — no manual cleanup needed.

**Example — switching from mstock to flattrade:**
```ini
# Before (mstock)
REDIRECT_URL = 'http://127.0.0.1:5000/mstock/callback'
BROKER_API_KEY = MA6718246
BROKER_PASSWORD = old_password
BROKER_TOTP_SECRET = OLD_SECRET

# After (flattrade) — only these lines change
REDIRECT_URL = 'http://127.0.0.1:5000/flattrade/callback'
BROKER_API_KEY = FZ40074:::your_api_key
BROKER_API_SECRET = your_api_secret
BROKER_PASSWORD = new_password
BROKER_TOTP_SECRET = NEW_SECRET
```

### Switching to an OAuth-only broker (e.g. zerodha)

1. Change `REDIRECT_URL` to `http://127.0.0.1:5000/zerodha/callback`
2. Log in via the OpenAlgo web UI (OAuth redirect flow) — this stores a zerodha token in the DB
3. Restart the signal engine — the scheduler detects zerodha as OAuth-only and reuses the stored token
4. If the stored token is for a different broker, startup fails with a clear error naming both the old and new broker

---

## Auto-login startup flow

```
openalgoctl.sh startup
  └─ openalgoscheduler.py startup
       1. get_broker_name()         → reads REDIRECT_URL or BROKER_NAME
       2. import broker.{name}.api.auth_api
       3a. has authenticate_with_totp?
           YES → generate TOTP → call authenticate_with_totp(password, totp)
                → store new token in DB
           NO  → retrieve existing DB token (OAuth broker)
                → check stored token's broker matches current broker
       4. verify_broker_auth(token) → call broker's get_margin_data()
       5. PASS → start signal engine
          FAIL → log error → sys.exit(1) → Telegram notification
```

---

## Required env vars

| Var | Required for | Notes |
|-----|-------------|-------|
| `REDIRECT_URL` | All brokers | Used to detect broker name |
| `BROKER_PASSWORD` | TOTP brokers | Plaintext; hashed before sending |
| `BROKER_TOTP_SECRET` | TOTP brokers | Base32 seed from authenticator app |
| `BROKER_API_KEY` | All brokers | Format varies by broker |
| `BROKER_API_SECRET` | flattrade, most brokers | Not needed for mstock |
| `BROKER_NAME` | Optional override | Overrides REDIRECT_URL detection |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Cannot determine broker name` | No REDIRECT_URL, no BROKER_NAME | Set `REDIRECT_URL` in `.env` |
| `Session invalid` on ftauth | Origin/Referer headers missing | Should be handled automatically; check that auth/session is reachable |
| `ftauth HTTP 4xx` | Wrong API key format | Verify `BROKER_API_KEY=CLIENT_ID:::API_KEY` for flattrade |
| `Broker changed from X to Y` | DB has token from old broker | For TOTP brokers: just restart (new token auto-generated). For OAuth: login via browser first |
| `BROKER_PASSWORD is required` | Env var not set | Add `BROKER_PASSWORD=...` to `.env` |
| `No active broker found during startup` | Token not yet stored | Complete OAuth login via browser, then restart |
| `stat=Not_Ok` / `emsg=...` | Wrong password or TOTP | Check BROKER_PASSWORD and BROKER_TOTP_SECRET |

---

## Running tests

```bash
PYTHONPATH=. uv run pytest signal_engine/tests/test_openalgoscheduler.py -v
```

Key test classes:
- `TestGetBrokerName` — broker resolution from REDIRECT_URL and BROKER_NAME
- `TestBrokerModuleContracts` — confirms mstock and flattrade expose `authenticate_with_totp`
- `TestBrokerSwitch` — end-to-end switching scenarios (mstock↔flattrade, to OAuth broker)
- `TestAutoLoginOAuthBroker` — OAuth fallback path (e.g. zerodha)
- `TestVerifyBrokerAuth` — token verification against broker funds API
