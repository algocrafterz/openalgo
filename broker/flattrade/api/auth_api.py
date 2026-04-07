import hashlib
import os
from urllib.parse import parse_qs, urlparse

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def sha256_hash(text):
    """Generate SHA256 hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def authenticate_with_totp(password, totp_code):
    """Programmatic TOTP login for Flattrade via headless OAuth flow.

    Uses the same ftauth endpoint as the browser login page — no vendor
    registration required. Flow:
      1. POST https://authapi.flattrade.in/ftauth
         Body: {UserName, Password (SHA256), PAN_DOB (TOTP), APIKey}
         Response: {emsg: "", RedirectURL: "...?code=XXXX"}
      2. Parse code from RedirectURL
      3. Exchange code for auth token via /trade/apitoken

    BROKER_API_KEY must be in format: CLIENT_ID:::API_KEY
      CLIENT_ID = your Flattrade trading account ID (e.g. FZ40074)
      API_KEY   = API key from your Flattrade developer account
    BROKER_API_SECRET = API secret from your Flattrade developer account

    Returns:
        tuple: (auth_token, feed_token, error_message)
    """
    full_api_key = os.getenv("BROKER_API_KEY", "")
    if ":::" not in full_api_key:
        return None, None, (
            "BROKER_API_KEY must be in format 'CLIENT_ID:::API_KEY' "
            "for programmatic Flattrade login (e.g. FZ40074:::abc123key)"
        )

    parts = full_api_key.split(":::", 1)
    client_id = parts[0].strip()
    api_key = parts[1].strip()
    api_secret = os.getenv("BROKER_API_SECRET", "").strip()

    if not client_id:
        return None, None, "CLIENT_ID missing from BROKER_API_KEY (format: CLIENT_ID:::API_KEY)"
    if not api_key:
        return None, None, "API_KEY missing from BROKER_API_KEY (format: CLIENT_ID:::API_KEY)"
    if not api_secret:
        return None, None, "BROKER_API_SECRET not set"
    if not password:
        return None, None, "BROKER_PASSWORD is required for programmatic login"
    if not totp_code:
        return None, None, "TOTP code is required"

    # Headers that mimic the browser SPA — required for session endpoint to return a SID
    auth_headers = {
        "Content-Type": "application/json",
        "Origin": "https://auth.flattrade.in",
        "Referer": "https://auth.flattrade.in/",
    }

    try:
        http_client = get_httpx_client()

        # Step 1: Get a session ID — browser does this before showing the login form
        session_resp = http_client.post(
            "https://authapi.flattrade.in/auth/session",
            headers=auth_headers,
            timeout=15,
        )
        sid = session_resp.text.strip().strip('"')
        if not sid:
            return None, None, "Failed to obtain session ID from auth/session"
        logger.info("Flattrade session established for client: %s", client_id)

        # Step 2: POST credentials to ftauth — same endpoint the browser login page uses
        ftauth_url = "https://authapi.flattrade.in/ftauth"
        ftauth_payload = {
            "UserName": client_id,
            "Password": sha256_hash(password),
            "PAN_DOB": totp_code,
            "APIKey": api_key,
            "Sid": sid,
        }
        logger.info("Flattrade ftauth login for client: %s", client_id)
        resp = http_client.post(ftauth_url, json=ftauth_payload, headers=auth_headers, timeout=30)

        if resp.status_code != 200:
            return None, None, f"ftauth HTTP {resp.status_code}: {resp.text[:300]}"

        data = resp.json()
        emsg = data.get("emsg", "")
        redirect_url = data.get("RedirectURL", "")

        if emsg == "DUPLICATE":
            # Already logged in — RedirectURL still contains the code
            logger.info("Flattrade ftauth: duplicate session, extracting code from RedirectURL")
        elif emsg:
            logger.error("Flattrade ftauth failed: %s", emsg)
            return None, None, emsg

        if not redirect_url:
            return None, None, "ftauth succeeded but no RedirectURL in response"

        # Step 2: Extract code from RedirectURL (e.g. http://host/flattrade/callback?code=XXXX)
        parsed = urlparse(redirect_url)
        qs = parse_qs(parsed.query)
        code_list = qs.get("code") or qs.get("request_code")
        if not code_list:
            return None, None, f"No code in RedirectURL: {redirect_url}"
        code = code_list[0]

        # Step 3: Exchange code for auth token
        security_hash = sha256_hash(f"{api_key}{code}{api_secret}")
        token_url = "https://authapi.flattrade.in/trade/apitoken"
        token_payload = {"api_key": api_key, "request_code": code, "api_secret": security_hash}
        token_resp = http_client.post(token_url, json=token_payload, timeout=30)

        if token_resp.status_code != 200:
            return None, None, f"token exchange HTTP {token_resp.status_code}: {token_resp.text[:300]}"

        token_data = token_resp.json()
        if token_data.get("stat") == "Ok" and token_data.get("token"):
            logger.info("Flattrade login successful for %s", client_id)
            return token_data["token"], None, None

        error = token_data.get("emsg") or token_data.get("message") or "token exchange failed"
        logger.error("Flattrade token exchange failed: %s", error)
        return None, None, error

    except Exception as e:
        logger.error("Flattrade login exception: %s", e)
        return None, None, str(e)


def authenticate_broker(code, password=None, totp_code=None):
    """
    Authenticate with Flattrade using OAuth flow
    """
    try:
        full_api_key = os.getenv("BROKER_API_KEY")
        logger.debug(f"Full API Key: {full_api_key}")  # Debug print

        # Split the API key to get the actual key part
        BROKER_API_KEY = full_api_key.split(":::")[1]
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")

        logger.debug(f"Using API Key: {BROKER_API_KEY}")  # Debug print
        logger.debug(f"Request Code: {code}")  # Debug print

        # Create the security hash as per Flattrade docs
        hash_input = f"{BROKER_API_KEY}{code}{BROKER_API_SECRET}"
        security_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        logger.debug(f"Hash Input: {hash_input}")  # Debug print
        logger.debug(f"Security Hash: {security_hash}")  # Debug print

        url = "https://authapi.flattrade.in/trade/apitoken"
        data = {"api_key": BROKER_API_KEY, "request_code": code, "api_secret": security_hash}

        logger.debug(f"Request Data: {data}")  # Debug print

        # Get the shared httpx client
        client = get_httpx_client()

        response = client.post(url, json=data)

        logger.debug(f"Response Status: {response.status_code}")  # Debug print
        logger.debug(f"Response Content: {response.text}")  # Debug print

        if response.status_code == 200:
            response_data = response.json()
            if response_data.get("stat") == "Ok" and "token" in response_data:
                return response_data["token"], None
            else:
                error_msg = response_data.get(
                    "emsg", "Authentication failed without specific error"
                )
                logger.error(f"Auth Error: {error_msg}")  # Debug print
                return None, error_msg
        else:
            try:
                error_detail = response.json()
                error_msg = f"API error: {error_detail.get('emsg', 'Unknown error')}"
            except:
                error_msg = f"API error: Status {response.status_code}, Response: {response.text}"
            logger.error(f"Request Error: {error_msg}")  # Debug print
            return None, error_msg

    except Exception as e:
        logger.debug(f"Exception: {e}")  # Debug print
        return None, f"An exception occurred: {str(e)}"


def authenticate_broker_oauth(code):
    try:
        BROKER_API_KEY = os.getenv("BROKER_API_KEY").split(":::")[1]  # Get only the API key part
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")

        # Create the security hash as per Flattrade docs
        # api_secret:SHA-256 hash of (api_key + request_token + api_secret)
        hash_input = f"{BROKER_API_KEY}{code}{BROKER_API_SECRET}"
        security_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        url = "https://authapi.flattrade.in/trade/apitoken"
        data = {"api_key": BROKER_API_KEY, "request_code": code, "api_secret": security_hash}

        # Get the shared httpx client
        client = get_httpx_client()

        response = client.post(url, json=data)

        if response.status_code == 200:
            response_data = response.json()
            if response_data.get("stat") == "Ok" and "token" in response_data:
                return response_data["token"], None
            else:
                return None, response_data.get(
                    "emsg", "Authentication failed without specific error"
                )
        else:
            error_detail = response.json()
            return None, f"API error: {error_detail.get('emsg', 'Unknown error')}"

    except Exception as e:
        return None, f"An exception occurred: {str(e)}"
