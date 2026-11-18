"""Configuración desde .env (sin dependencias extra)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    app_id: str
    key_path: Path
    redirect_url: str
    db_path: Path
    country: str
    history_days: int
    consent_days: int

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv(ROOT / ".env")
        missing = [k for k in ("ENABLE_APP_ID", "ENABLE_REDIRECT_URL") if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                f"Faltan variables en .env: {', '.join(missing)}. Copia .env.example a .env y rellénalo."
            )
        key_path = ROOT / os.environ.get("ENABLE_KEY_PATH", "secrets/wally.pem")
        if not key_path.exists():
            raise SystemExit(f"No encuentro la clave privada en {key_path}.")
        return cls(
            app_id=os.environ["ENABLE_APP_ID"],
            key_path=key_path,
            redirect_url=os.environ["ENABLE_REDIRECT_URL"],
            db_path=ROOT / os.environ.get("WALLY_DB", "data/wally.db"),
            country=os.environ.get("WALLY_COUNTRY", "DK"),
            history_days=int(os.environ.get("WALLY_HISTORY_DAYS", "730")),
            consent_days=int(os.environ.get("WALLY_CONSENT_DAYS", "180")),
        )

    def private_key(self) -> str:
        pem = self.key_path.read_text(encoding="utf-8")
        if "BEGIN" not in pem or "PRIVATE KEY" not in pem:
            raise SystemExit(f"{self.key_path} no parece una clave privada PEM.")
        return pem
