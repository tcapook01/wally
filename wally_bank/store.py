"""Almacenamiento local en SQLite: sesiones, cuentas y movimientos."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .normalize import Tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    aspsp_name    TEXT NOT NULL,
    aspsp_country TEXT NOT NULL,
    valid_until   TEXT,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS accounts (
    uid           TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    aspsp_name    TEXT NOT NULL,
    name          TEXT,
    iban          TEXT,
    currency      TEXT,
    last_sync_at  TEXT,
    balance       REAL,
    balance_at    TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    account_uid   TEXT NOT NULL REFERENCES accounts(uid),
    status        TEXT NOT NULL,
    booking_date  TEXT,
    value_date    TEXT,
    date          TEXT NOT NULL,
    amount        REAL NOT NULL,
    currency      TEXT NOT NULL,
    counterparty  TEXT,
    description   TEXT,
    merchant_key  TEXT,
    type          TEXT,            -- gasto | ingreso | traspaso (lo rellena el Módulo 3)
    category_id   INTEGER,         -- lo rellena el Módulo 2
    raw_json      TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_account_date ON transactions(account_uid, date);
CREATE INDEX IF NOT EXISTS ix_tx_merchant ON transactions(merchant_key);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ sesiones
    def save_session(self, session: dict) -> None:
        aspsp = session.get("aspsp", {})
        access = session.get("access", {})
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?, 'active')",
                (session["session_id"], aspsp.get("name"), aspsp.get("country"),
                 access.get("valid_until"), _now()),
            )
            for acc in session.get("accounts", []):
                acc_id = acc.get("account_id") or {}
                self.db.execute(
                    """INSERT INTO accounts(uid, session_id, aspsp_name, name, iban, currency)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(uid) DO UPDATE SET session_id=excluded.session_id,
                         name=excluded.name, iban=excluded.iban, currency=excluded.currency""",
                    (acc["uid"], session["session_id"], aspsp.get("name"),
                     acc.get("name") or acc.get("product") or acc.get("details"),
                     acc_id.get("iban"), acc.get("currency")),
                )

    def accounts(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT a.*, s.valid_until, s.status AS session_status
               FROM accounts a JOIN sessions s USING(session_id)
               ORDER BY a.aspsp_name, a.name"""
        ).fetchall()

    def mark_session(self, session_id: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE sessions SET status=? WHERE session_id=?", (status, session_id))

    # --------------------------------------------------------- movimientos
    def last_booked_date(self, account_uid: str) -> str | None:
        row = self.db.execute(
            "SELECT MAX(date) FROM transactions WHERE account_uid=? AND status='BOOK'",
            (account_uid,),
        ).fetchone()
        return row[0]

    def upsert(self, txs: list[Tx]) -> tuple[int, int]:
        """Inserta o actualiza. Devuelve (nuevos, actualizados)."""
        new = upd = 0
        now = _now()
        with self.db:
            for t in txs:
                exists = self.db.execute("SELECT 1 FROM transactions WHERE id=?", (t.id,)).fetchone()
                self.db.execute(
                    """INSERT INTO transactions(id, account_uid, status, booking_date, value_date, date,
                         amount, currency, counterparty, description, merchant_key, raw_json,
                         first_seen_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                         booking_date=excluded.booking_date, value_date=excluded.value_date,
                         date=excluded.date, amount=excluded.amount, counterparty=excluded.counterparty,
                         description=excluded.description, merchant_key=excluded.merchant_key,
                         raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
                    (t.id, t.account_uid, t.status, t.booking_date, t.value_date, t.date, t.amount,
                     t.currency, t.counterparty, t.description, t.merchant_key, t.raw_json, now, now),
                )
                if exists:
                    upd += 1
                else:
                    new += 1
        return new, upd

    def drop_stale_pending(self, account_uid: str, seen_ids: set[str]) -> int:
        """El banco devuelve en cada consulta todos los pendientes vigentes.
        Un pendiente que ya no aparece se ha contabilizado (y llega como BOOK) o se ha anulado."""
        rows = self.db.execute(
            "SELECT id FROM transactions WHERE account_uid=? AND status='PDNG'", (account_uid,)
        ).fetchall()
        stale = [r["id"] for r in rows if r["id"] not in seen_ids]
        with self.db:
            self.db.executemany("DELETE FROM transactions WHERE id=?", [(i,) for i in stale])
        return len(stale)

    def set_balance(self, account_uid: str, amount: float | None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE accounts SET balance=?, balance_at=?, last_sync_at=? WHERE uid=?",
                (amount, _now(), _now(), account_uid),
            )

    def summary(self, since: str) -> dict:
        q = lambda sql: self.db.execute(sql, (since,)).fetchall()
        totals = q("""SELECT currency,
                        ROUND(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),2) AS entra,
                        ROUND(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),2) AS sale,
                        COUNT(*) AS n
                      FROM transactions WHERE date>=? GROUP BY currency""")
        top = q("""SELECT COALESCE(NULLIF(merchant_key,''),'(sin nombre)') AS k, currency,
                      ROUND(-SUM(amount),2) AS total, COUNT(*) AS n
                   FROM transactions WHERE date>=? AND amount<0
                   GROUP BY k, currency ORDER BY total DESC LIMIT 10""")
        return {"totals": totals, "top": top}
