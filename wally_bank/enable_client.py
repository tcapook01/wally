"""Cliente mínimo de la API de Enable Banking (solo lectura).

Autenticación: cada petición lleva un JWT firmado con la clave privada RSA de la
aplicación (RS256). Cabecera: kid = Application ID. Claims: iss, aud, iat, exp.
La vida máxima del token es 24 h; aquí usamos 1 h y lo renovamos solo.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

import jwt
import requests

API_URL = "https://api.enablebanking.com"
JWT_LIFETIME = 3600  # segundos (máximo permitido: 86400)


class EnableBankingError(Exception):
    def __init__(self, status: int, body: Any, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"{status} en {path}: {body}")

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429

    @property
    def is_expired_session(self) -> bool:
        text = str(self.body).upper()
        return self.status in (401, 403) and ("EXPIRED" in text or "SESSION" in text)


class EnableClient:
    def __init__(
        self,
        app_id: str,
        private_key_pem: str | bytes,
        base_url: str = API_URL,
        http: requests.Session | None = None,
        timeout: float = 30.0,
    ):
        self.app_id = app_id
        self._key = private_key_pem
        self.base_url = base_url.rstrip("/")
        self.http = http or requests.Session()
        self.timeout = timeout
        self._jwt: str | None = None
        self._jwt_exp = 0

    # ---------------------------------------------------------------- auth
    def token(self) -> str:
        now = int(time.time())
        if self._jwt and now < self._jwt_exp - 60:
            return self._jwt
        exp = now + JWT_LIFETIME
        self._jwt = jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": exp},
            self._key,
            algorithm="RS256",
            headers={"kid": self.app_id},
        )
        self._jwt_exp = exp
        return self._jwt

    def _request(self, method: str, path: str, **kw) -> Any:
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        resp = self.http.request(
            method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout, **kw
        )
        if not resp.ok:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise EnableBankingError(resp.status_code, body, path)
        return resp.json() if resp.content else {}

    # ----------------------------------------------------------- endpoints
    def application(self) -> dict:
        """Datos de tu aplicación. Sirve para comprobar que clave e ID funcionan."""
        return self._request("GET", "/application")

    def aspsps(self, country: str, psu_type: str = "personal") -> list[dict]:
        data = self._request("GET", "/aspsps", params={"country": country, "psu_type": psu_type})
        return data.get("aspsps", [])

    def start_auth(
        self,
        aspsp_name: str,
        country: str,
        redirect_url: str,
        valid_until: str,
        state: str,
        psu_type: str = "personal",
        language: str = "en",
    ) -> dict:
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
            "language": language,
        }
        return self._request("POST", "/auth", json=body)

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def delete_session(self, session_id: str) -> dict:
        return self._request("DELETE", f"/sessions/{session_id}")

    def account_details(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/details")

    def balances(self, account_uid: str) -> list[dict]:
        return self._request("GET", f"/accounts/{account_uid}/balances").get("balances", [])

    def transactions(
        self,
        account_uid: str,
        date_from: str | None = None,
        date_to: str | None = None,
        transaction_status: str | None = None,
        strategy: str | None = None,
    ) -> Iterator[dict]:
        """Devuelve todos los movimientos, siguiendo la paginación (continuation_key)."""
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if transaction_status:
            params["transaction_status"] = transaction_status
        if strategy:
            params["strategy"] = strategy
        while True:
            page = self._request("GET", f"/accounts/{account_uid}/transactions", params=params)
            yield from page.get("transactions", [])
            key = page.get("continuation_key")
            if not key:
                break
            params["continuation_key"] = key
