#!/usr/bin/env python3
"""Wally — conexión con tus bancos vía Enable Banking.

Uso:
  python wally.py check                      comprueba clave e Application ID
  python wally.py banks [--search danske]    lista los bancos disponibles en tu país
  python wally.py connect "Danske Bank"      conecta un banco (abre el navegador)
  python wally.py accounts                   cuentas conectadas y caducidad del permiso
  python wally.py sync                       descarga movimientos nuevos y saldos
  python wally.py summary [--days 30]        resumen rápido de entradas y salidas
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone

import requests

from wally_bank.config import Config
from wally_bank.enable_client import EnableBankingError, EnableClient
from wally_bank.store import Store
from wally_bank.sync import begin_connect, find_aspsp, finish_connect, sync_account

HELLO = "Wally"


def _client(cfg: Config) -> EnableClient:
    return EnableClient(cfg.app_id, cfg.private_key())


def _money(x: float | None, cur: str | None = "") -> str:
    if x is None:
        return "—"
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {cur or ''}".strip()


def cmd_check(cfg: Config, _args) -> None:
    app = _client(cfg).application()
    print(f"{HELLO}: conexión con Enable Banking correcta.")
    print(f"  Aplicación: {app.get('name')}  ·  activa: {app.get('active')}")
    countries = app.get("countries") or []
    if countries:
        print(f"  Países: {', '.join(countries)}")
    if app.get("active") is False:
        print("  Aún no está activa: en el Control Panel usa 'Activate by linking accounts'.")


def cmd_banks(cfg: Config, args) -> None:
    aspsps = _client(cfg).aspsps(args.country or cfg.country)
    if args.search:
        aspsps = find_aspsp(aspsps, args.search)
    for a in sorted(aspsps, key=lambda a: a.get("name", "")):
        days = a.get("maximum_consent_validity")
        days = f"{int(days) // 86400} días" if days else "?"
        print(f"  {a.get('name'):<40} {a.get('country')}  permiso máx.: {days}")
    print(f"{len(aspsps)} bancos.")


def cmd_connect(cfg: Config, args) -> None:
    client, store = _client(cfg), Store(cfg.db_path)
    country = args.country or cfg.country
    matches = find_aspsp(client.aspsps(country), args.bank)
    if not matches:
        sys.exit(f"No encuentro '{args.bank}' en {country}. Prueba: python wally.py banks --search {args.bank.split()[0]}")
    if len(matches) > 1:
        print("Hay varios bancos con ese nombre:")
        for i, a in enumerate(matches, 1):
            print(f"  {i}. {a['name']}")
        aspsp = matches[int(input("Elige número: ")) - 1]
    else:
        aspsp = matches[0]

    start = begin_connect(client, aspsp, cfg.redirect_url, cfg.consent_days)
    print(f"\n{HELLO}: vamos a conectar {aspsp['name']}.")
    print("1. Se abre la página de tu banco. Autoriza el acceso (solo lectura).")
    print("2. Al terminar, el navegador irá a tu página de callback.")
    print("3. Copia la dirección COMPLETA de la barra del navegador y pégala aquí.\n")
    print(start["url"], "\n")
    webbrowser.open(start["url"])
    redirected = input("URL de vuelta: ")

    session = finish_connect(client, store, redirected, start["state"])
    accounts = session.get("accounts", [])
    valid = session.get("access", {}).get("valid_until", "?")
    print(f"\nConectado. {len(accounts)} cuenta(s). Permiso válido hasta {valid[:10]}.")
    print("Descargando el histórico ahora (con el permiso recién dado el banco suele dar más meses)...")
    for acc in accounts:
        label = f"{aspsp['name']} · {acc.get('name') or acc.get('product') or acc['uid'][:8]}"
        _print_sync(sync_account(client, store, acc["uid"], label, cfg.history_days))


def cmd_accounts(cfg: Config, _args) -> None:
    rows = Store(cfg.db_path).accounts()
    if not rows:
        print("No hay cuentas. Usa: python wally.py connect \"Danske Bank\"")
        return
    today = datetime.now(timezone.utc)
    for r in rows:
        warn = ""
        if r["valid_until"]:
            left = (datetime.fromisoformat(r["valid_until"].replace("Z", "+00:00")) - today).days
            warn = f"  ⚠ renovar en {left} días" if left <= 7 else f"  permiso: {left} días"
        print(f"  {r['aspsp_name']:<14} {r['name'] or '':<24} {_money(r['balance'], r['currency']):>16}"
              f"  última sync: {(r['last_sync_at'] or '—')[:16]}{warn}")


def _print_sync(res) -> None:
    if res.error:
        print(f"  ✗ {res.account}: {res.error}")
        return
    extra = f" · {', '.join(res.notes)}" if res.notes else ""
    print(f"  ✓ {res.account}: {res.new} nuevos, {res.updated} actualizados, "
          f"{res.pending_dropped} pendientes resueltos · saldo {_money(res.balance, res.currency)}{extra}")


def cmd_sync(cfg: Config, _args) -> None:
    client, store = _client(cfg), Store(cfg.db_path)
    rows = [r for r in store.accounts() if r["session_status"] == "active"]
    if not rows:
        print("No hay cuentas activas que sincronizar.")
        return
    print(f"{HELLO}: sincronizando {len(rows)} cuenta(s)...")
    for r in rows:
        res = sync_account(client, store, r["uid"], f"{r['aspsp_name']} · {r['name'] or r['uid'][:8]}",
                           cfg.history_days)
        if res.error and "caducado" in res.error:
            store.mark_session(r["session_id"], "expired")
        _print_sync(res)


def cmd_summary(cfg: Config, args) -> None:
    since = (date.today() - timedelta(days=args.days)).isoformat()
    s = Store(cfg.db_path).summary(since)
    print(f"{HELLO}: últimos {args.days} días (traspasos entre tus cuentas aún incluidos)\n")
    for t in s["totals"]:
        print(f"  {t['currency']}: entra {_money(t['entra'])} · sale {_money(t['sale'])} · {t['n']} movimientos")
    print("\n  Dónde se va más dinero:")
    for t in s["top"]:
        print(f"    {t['k'][:32]:<32} {_money(t['total'], t['currency']):>16}  ({t['n']}x)")


def main() -> None:
    p = argparse.ArgumentParser(prog="wally", description="Wally: conexión bancaria")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    b = sub.add_parser("banks"); b.add_argument("--search"); b.add_argument("--country")
    c = sub.add_parser("connect"); c.add_argument("bank"); c.add_argument("--country")
    sub.add_parser("accounts")
    sub.add_parser("sync")
    s = sub.add_parser("summary"); s.add_argument("--days", type=int, default=30)
    args = p.parse_args()
    cfg = Config.load()
    handler = {"check": cmd_check, "banks": cmd_banks, "connect": cmd_connect,
               "accounts": cmd_accounts, "sync": cmd_sync, "summary": cmd_summary}[args.cmd]
    try:
        handler(cfg, args)
    except EnableBankingError as e:
        sys.exit(f"Error de Enable Banking ({e.status}): {e.body}")
    except requests.exceptions.RequestException as e:
        sys.exit(f"No puedo conectar con api.enablebanking.com: {e.__class__.__name__}. ¿Hay internet?")


if __name__ == "__main__":
    main()
