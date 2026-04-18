import base64
import datetime
import os
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")


def _load_private_key():
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign_request(method: str, path: str) -> dict:
    timestamp_ms = str(int(datetime.datetime.now(datetime.UTC).timestamp() * 1000))
    full_path = "/trade-api/v2" + path
    message = (timestamp_ms + method.upper() + full_path).encode("utf-8")
    private_key = _load_private_key()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "Content-Type": "application/json",
    }


def get(path: str, params: dict = None) -> dict:
    # Strip query params from path before signing
    headers = _sign_request("GET", path.split("?")[0])
    resp = requests.get(BASE_URL + path, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_balance() -> dict:
    return get("/portfolio/balance")


def get_markets(params: dict = None) -> dict:
    return get("/markets", params=params)


def get_market(ticker: str) -> dict:
    return get(f"/markets/{ticker}")


def get_orderbook(ticker: str) -> dict:
    return get(f"/markets/{ticker}/orderbook")
