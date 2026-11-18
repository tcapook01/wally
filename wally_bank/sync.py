"""Conexión de bancos y sincronización de movimientos."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from .enable_client import EnableBankingError, EnableClient
from .normalize import normalize
from .store import Store

# Preferencia de saldo: disponible > contable (los códigos varían según el banco)
BALANCE_PREFERENCE = ["ITAV", "CLAV", "XPCD", "ITBD", "CLBD", "OPBD"]
OVERLAP_DAYS = 10  # se vuelve a pedir un margen para captar correcciones del banco


# --------------------------------------------------------------- conexión
def consent_valid_until(aspsp: dict, wanted_days: int, now: datetime | None = None) -> str:
    """Pide el máximo que acepte el banco, sin pasar de wanted_days."""
    now = now or datetime.now(timezone.utc)
    max_secs = aspsp.get("maximum_consent_validity")
    secs = wanted_days * 86400
    if isinstance(max_secs, (int, float)) and max_secs > 0:
        secs = min(secs, int(max_secs))
    return (now + timedelta(seconds=secs - 60)).isoformat(timespec="seconds")


def find_aspsp(aspsps: list[dict], query: str) -> list[dict]:
    q = query.lower().strip()
    exact = [a for a in aspsps if a.get("name", "").lower() == q]
    return exact or [a for a in aspsps if q in a.get("name", "").lower()]


def parse_redirect(url: str) -> dict:
    """Extrae code/state/error de la URL a la que te devuelve el banco."""
    qs = parse_qs(urlparse(url.strip()).query)
    return {k: v[0] for k, v in qs.items()}


def begin_connect(client: EnableClient, aspsp: dict, redirect_url: str, consent_days: int) -> dict:
    state = str(uuid.uuid4())
    auth = client.start_auth(
        aspsp_name=aspsp["name"],
        country=aspsp["country"],
        redirect_url=redirect_url,
        valid_until=consent_valid_until(aspsp, consent_days),
        state=state,
    )
    return {"url": auth["url"], "state": state}


def finish_connect(client: EnableClient, store: Store, redirected_url: str, expected_state: str) -> dict:
    params = parse_redirect(redirected_url)
    if "error" in params:
        raise SystemExit(f"El banco devolvió un error: {params.get('error')} {params.get('error_description','')}")
    if params.get("state") != expected_state:
        raise SystemExit("El parámetro 'state' no coincide. Repite la conexión desde el principio.")
    if "code" not in params:
        raise SystemExit("No encuentro 'code' en la URL. Copia la dirección completa de la barra del navegador.")
    session = client.create_session(params["code"])
    store.save_session(session)
    return session


# --------------------------------------------------------- sincronización
@dataclass
class SyncResult:
    account: str
    new: int = 0
    updated: int = 0
    pending_dropped: int = 0
    balance: float | None = None
    currency: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


def pick_balance(balances: list[dict]) -> tuple[float | None, str | None]:
    by_type = {b.get("balance_type"): b for b in balances}
    for t in BALANCE_PREFERENCE:
        if t in by_type:
            amt = by_type[t].get("balance_amount", {})
            return float(amt.get("amount")), amt.get("currency")
    if balances:
        amt = balances[0].get("balance_amount", {})
        return float(amt.get("amount")), amt.get("currency")
    return None, None


def sync_account(client: EnableClient, store: Store, account_uid: str, label: str,
                 history_days: int, today: date | None = None) -> SyncResult:
    res = SyncResult(account=label)
    today = today or date.today()
    last = store.last_booked_date(account_uid)
    if last:
        date_from = (date.fromisoformat(last[:10]) - timedelta(days=OVERLAP_DAYS)).isoformat()
        strategy = None
    else:
        date_from = (today - timedelta(days=history_days)).isoformat()
        strategy = "longest"  # primera vez: el máximo histórico que dé el banco
        res.notes.append(f"primera sincronización desde {date_from}")
    try:
        try:
            raw = list(client.transactions(account_uid, date_from=date_from, strategy=strategy))
        except EnableBankingError as e:
            if strategy and e.status == 422:  # el banco no admite 'longest'
                raw = list(client.transactions(account_uid, date_from=date_from))
            else:
                raise
        txs = [normalize(account_uid, t) for t in raw]
        res.new, res.updated = store.upsert(txs)
        res.pending_dropped = store.drop_stale_pending(
            account_uid, {t.id for t in txs if t.status == "PDNG"}
        )
        res.balance, res.currency = pick_balance(client.balances(account_uid))
        store.set_balance(account_uid, res.balance)
    except EnableBankingError as e:
        if e.is_rate_limited:
            res.error = "el banco ha llegado a su límite de consultas de hoy; prueba en unas horas"
        elif e.is_expired_session:
            res.error = "el permiso ha caducado; vuelve a conectar el banco"
        else:
            res.error = str(e)
    return res
