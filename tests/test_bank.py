"""Pruebas sin red: un servidor falso imita las respuestas de Enable Banking."""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wally_bank.enable_client import EnableBankingError, EnableClient  # noqa: E402
from wally_bank.normalize import merchant_key, normalize  # noqa: E402
from wally_bank.store import Store  # noqa: E402
from wally_bank.sync import (  # noqa: E402
    consent_valid_until, finish_connect, parse_redirect, pick_balance, sync_account,
)

APP_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


class FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.ok = 200 <= status < 300
        self.content = json.dumps(data).encode()
        self.text = self.content.decode()

    def json(self):
        return self._data


class FakeBank:
    """Imita /sessions, /accounts/{uid}/transactions (paginado) y /balances."""

    def __init__(self):
        self.calls = []
        self.pages = {}
        self.balances = [{"balance_type": "CLBD", "balance_amount": {"amount": "900.00", "currency": "DKK"}},
                         {"balance_type": "ITAV", "balance_amount": {"amount": "850.50", "currency": "DKK"}}]
        self.status_override = None

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), headers))
        if self.status_override:
            return FakeResp(*self.status_override)
        path = url.split("api.enablebanking.com")[1]
        if path == "/sessions":
            return FakeResp(200, {"session_id": "sess-1", "aspsp": {"name": "Danske Bank", "country": "DK"},
                                  "access": {"valid_until": "2027-05-17T10:00:00+00:00"},
                                  "accounts": [{"uid": "acc-1", "name": "Lønkonto", "currency": "DKK",
                                                "account_id": {"iban": "DK5000400440116243"}}]})
        if path.endswith("/balances"):
            return FakeResp(200, {"balances": self.balances})
        if path.endswith("/transactions"):
            key = (params or {}).get("continuation_key", "first")
            return FakeResp(200, self.pages[key])
        return FakeResp(404, {"error": "not found"})


def tx(ref, amount, ind, status="BOOK", d="2026-11-10", name="NETTO", info=None):
    t = {"transaction_amount": {"amount": amount, "currency": "DKK"}, "credit_debit_indicator": ind,
         "status": status, "booking_date": d if status == "BOOK" else None, "transaction_date": d,
         "remittance_information": info or [f"VISA 4571 {name} KBH 10.11"],
         ("creditor" if ind == "DBIT" else "debtor"): {"name": name}}
    if ref:
        t["entry_reference"] = ref
    return t


# ------------------------------------------------------------------ tests
def test_jwt_matches_enable_banking_spec(keys):
    priv, pub = keys
    tok = EnableClient(APP_ID, priv).token()
    head = jwt.get_unverified_header(tok)
    assert head["alg"] == "RS256" and head["kid"] == APP_ID and head["typ"] == "JWT"
    claims = jwt.decode(tok, pub, algorithms=["RS256"], audience="api.enablebanking.com")
    assert claims["iss"] == "enablebanking.com"
    assert 0 < claims["exp"] - claims["iat"] <= 86400


def test_pagination_follows_continuation_key(keys):
    bank = FakeBank()
    bank.pages = {"first": {"transactions": [tx("a", "10", "DBIT")], "continuation_key": "p2"},
                  "p2": {"transactions": [tx("b", "20", "DBIT")], "continuation_key": None}}
    c = EnableClient(APP_ID, keys[0], http=bank)
    assert [t["entry_reference"] for t in c.transactions("acc-1")] == ["a", "b"]
    assert bank.calls[1][2]["continuation_key"] == "p2"
    assert bank.calls[0][3]["Authorization"].startswith("Bearer ")


def test_normalize_signs_and_merchant_key():
    out = normalize("acc-1", tx("r1", "125.50", "DBIT"))
    assert out.amount == -125.50 and out.counterparty == "NETTO" and out.merchant_key == "netto"
    inc = normalize("acc-1", tx("r2", "30000", "CRDT", name="ACME ApS", info=["Løn november"]))
    assert inc.amount == 30000 and inc.counterparty == "ACME ApS"
    assert merchant_key("VISA 1234 NETTO KBH 14.11") == "netto kbh"


def test_hash_id_when_bank_gives_no_reference():
    a = normalize("acc-1", tx(None, "50", "DBIT", d="2026-11-01"))
    b = normalize("acc-1", tx(None, "50", "DBIT", d="2026-11-01"))
    c = normalize("acc-1", tx(None, "50", "DBIT", d="2026-11-02"))
    assert a.id == b.id != c.id


def test_sync_dedup_and_pending_to_booked(tmp_path, keys):
    bank = FakeBank()
    store = Store(tmp_path / "w.db")
    client = EnableClient(APP_ID, keys[0], http=bank)
    finish_connect(client, store, "https://x.github.io/wally/callback/?code=abc&state=s1", "s1")

    # 1ª sync: un gasto contabilizado y uno pendiente (sin referencia, como suele pasar)
    bank.pages = {"first": {"transactions": [tx("r1", "100", "DBIT"),
                                             tx(None, "45", "DBIT", status="PDNG", d="2026-11-17", name="7-ELEVEN")]}}
    r1 = sync_account(client, store, "acc-1", "Danske", 730, today=date(2026, 11, 18))
    assert (r1.new, r1.updated, r1.error) == (2, 0, None)
    assert r1.balance == 850.50  # prefiere el disponible (ITAV)
    assert bank.calls[-2][2].get("strategy") == "longest"

    # 2ª sync: el pendiente ya está contabilizado (con referencia nueva); r1 vuelve por el solape
    bank.pages = {"first": {"transactions": [tx("r1", "100", "DBIT"),
                                             tx("r2", "45", "DBIT", d="2026-11-18", name="7-ELEVEN")]}}
    r2 = sync_account(client, store, "acc-1", "Danske", 730, today=date(2026, 11, 19))
    assert (r2.new, r2.updated, r2.pending_dropped) == (1, 1, 1)
    rows = store.db.execute("SELECT status, amount FROM transactions ORDER BY date").fetchall()
    assert [(r["status"], r["amount"]) for r in rows] == [("BOOK", -100.0), ("BOOK", -45.0)]
    # incremental: pide desde la última fecha contabilizada menos el margen
    assert bank.calls[-2][2]["date_from"] == "2026-10-31" and "strategy" not in bank.calls[-2][2]


def test_rate_limit_is_reported_not_crashing(tmp_path, keys):
    bank = FakeBank()
    store = Store(tmp_path / "w.db")
    client = EnableClient(APP_ID, keys[0], http=bank)
    finish_connect(client, store, "https://x/cb?code=abc&state=s", "s")
    bank.status_override = (429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
    res = sync_account(client, store, "acc-1", "Danske", 730)
    assert "límite" in res.error


def test_state_mismatch_and_bank_error_are_rejected(tmp_path, keys):
    store = Store(tmp_path / "w.db")
    client = EnableClient(APP_ID, keys[0], http=FakeBank())
    with pytest.raises(SystemExit, match="state"):
        finish_connect(client, store, "https://x/cb?code=abc&state=OTRO", "s")
    with pytest.raises(SystemExit, match="error"):
        finish_connect(client, store, "https://x/cb?error=access_denied&state=s", "s")


def test_consent_is_capped_by_bank_maximum():
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    assert consent_valid_until({"maximum_consent_validity": 90 * 86400}, 180, now).startswith("2027-02-15")
    assert consent_valid_until({}, 180, now).startswith("2027-05-16")


def test_helpers():
    assert parse_redirect("https://a/b?code=X&state=Y") == {"code": "X", "state": "Y"}
    assert pick_balance([]) == (None, None)
    assert EnableBankingError(429, {}, "/x").is_rate_limited
