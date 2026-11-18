"""Convierte un movimiento de Enable Banking en una fila homogénea para Wally."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


@dataclass
class Tx:
    id: str
    account_uid: str
    status: str            # BOOK (contabilizado) | PDNG (pendiente) | otro
    booking_date: str | None
    value_date: str | None
    date: str              # la mejor fecha disponible (para ordenar y presupuestar)
    amount: float          # con signo: negativo = sale dinero
    currency: str
    counterparty: str
    description: str
    merchant_key: str      # clave normalizada para la caché de categorías (Módulo 2)
    raw_json: str


_NOISE = re.compile(
    r"\b(visa|mastercard|dankort|mc|debit|kortkøb|kort|card|purchase|pos|nets|"
    r"betaling|payment|køb|mobilepay)\b",
    re.IGNORECASE,
)
_DIGITS_DATES = re.compile(r"[\d]+([./:-][\d]+)*")
_PUNCT = re.compile(r"[^\wæøåäöüéèáíóúñ ]+", re.IGNORECASE)


def merchant_key(text: str) -> str:
    """'VISA 1234 NETTO KBH 14.11' -> 'netto kbh'. Primera versión; se afina en el Módulo 2."""
    t = _NOISE.sub(" ", text or "")
    t = _DIGITS_DATES.sub(" ", t)
    t = _PUNCT.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t[:60]


def _description(tx: dict) -> str:
    info = tx.get("remittance_information") or []
    if isinstance(info, str):
        info = [info]
    parts = [p.strip() for p in info if p and p.strip()]
    if tx.get("note"):
        parts.append(str(tx["note"]).strip())
    return " | ".join(parts)


def _counterparty(tx: dict, is_debit: bool) -> str:
    party = (tx.get("creditor") if is_debit else tx.get("debtor")) or {}
    return (party.get("name") or "").strip()


def tx_id(account_uid: str, tx: dict, amount: float, date: str, desc: str) -> str:
    """Id estable: la referencia del banco si existe; si no, un hash del contenido."""
    ref = tx.get("entry_reference") or tx.get("transaction_id")
    if ref:
        return f"{account_uid}:{ref}"
    basis = f"{account_uid}|{date}|{amount:.2f}|{merchant_key(desc)}|{tx.get('status')}"
    return f"{account_uid}:h:{hashlib.sha256(basis.encode()).hexdigest()[:24]}"


def normalize(account_uid: str, tx: dict) -> Tx:
    amt = tx.get("transaction_amount") or {}
    value = float(amt.get("amount", 0))
    is_debit = tx.get("credit_debit_indicator") == "DBIT"
    signed = -abs(value) if is_debit else abs(value)
    date = tx.get("booking_date") or tx.get("transaction_date") or tx.get("value_date") or ""
    desc = _description(tx)
    cp = _counterparty(tx, is_debit)
    return Tx(
        id=tx_id(account_uid, tx, signed, date, desc or cp),
        account_uid=account_uid,
        status=tx.get("status") or "",
        booking_date=tx.get("booking_date"),
        value_date=tx.get("value_date"),
        date=date,
        amount=round(signed, 2),
        currency=amt.get("currency", ""),
        counterparty=cp,
        description=desc,
        merchant_key=merchant_key(cp or desc),
        raw_json=json.dumps(tx, ensure_ascii=False, sort_keys=True),
    )
