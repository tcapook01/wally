# Wally · Fase 1: conexión con el banco

Wally lee tus cuentas de Danske Bank y Revolut a través de Enable Banking (PSD2, solo lectura) y guarda los movimientos en una base de datos local (`data/wally.db`). Nunca mueve dinero, y tu contraseña del banco nunca pasa por Wally.

## 1. Registrar la aplicación en Enable Banking

En el Control Panel ve a **API applications → Add a new application**:

| Campo | Qué poner |
| --- | --- |
| Environment | **Production**. Sandbox solo sirve para el banco de pruebas (Mock ASPSP). |
| Clave | **Generate in the browser (SubtleCrypto) and export private key**. Al registrar se descarga un `.pem`: guárdalo, no se vuelve a mostrar. |
| Application name | `Wally` |
| Allowed redirect URLs | `https://TU-USUARIO.github.io/wally/callback/` (paso 2). En producción tiene que ser `https://` y no vale `localhost`. |
| Application description | `Personal finance app for my own accounts (personal use)` |
| Email | El tuyo |
| Privacy / Terms URLs | En modo restringido no se revisan: puedes poner la URL de tu repositorio o la misma del callback. |

Después, en la lista de aplicaciones, usa **Activate by linking accounts** y vincula Danske Bank y Revolut. La app quedará como *Restricted · Active*, que es lo correcto para uso personal: solo verá las cuentas que vincules ahí.

## 2. Publicar la página de callback (5 minutos)

Cuando autorizas en tu banco, este te devuelve a una URL con un `code`. Esa página solo te muestra la dirección para que la copies; no envía nada a ningún sitio.

1. Crea un repositorio público en GitHub llamado `wally`.
2. Sube la carpeta `callback/` (con su `index.html`).
3. En **Settings → Pages**, elige *Deploy from a branch*, `main` y la carpeta `/ (root)`.
4. En un minuto estará en `https://TU-USUARIO.github.io/wally/callback/`.

El `code` de esa URL es de un solo uso y no sirve sin tu clave privada.

## 3. Instalar y configurar

```bash
cd wally
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # rellena ENABLE_APP_ID y ENABLE_REDIRECT_URL
mkdir -p secrets && mv ~/Downloads/*.pem secrets/wally.pem
```

La clave tiene que empezar por `-----BEGIN PRIVATE KEY-----`. Si empieza por `BEGIN RSA PRIVATE KEY`, conviértela así:
`openssl pkcs8 -topk8 -nocrypt -in vieja.pem -out secrets/wally.pem`

## 4. Usarlo

```bash
python wally.py check                     # ¿funcionan clave e Application ID?
python wally.py banks --search danske     # nombre exacto del banco en Enable Banking
python wally.py connect "Danske Bank"     # autoriza en el banco y pega la URL de vuelta
python wally.py connect "Revolut"
python wally.py accounts                  # cuentas, saldos y días de permiso restantes
python wally.py sync                      # movimientos nuevos (úsalo 2–4 veces al día como máximo)
python wally.py summary --days 30
```

## Cómo funciona por dentro

- **Autenticación:** cada petición lleva un JWT RS256 firmado con tu clave (`kid` = Application ID, válido 1 h, se renueva solo).
- **Permiso:** se pide el máximo que acepte cada banco, hasta 180 días. `accounts` avisa cuando quedan 7 días o menos.
- **Histórico:** justo después de conectar se descargan hasta 2 años (`strategy=longest`). Muchos bancos solo dan más de 90 días en ese momento, recién autorizado.
- **Incremental:** cada `sync` pide desde la última fecha contabilizada menos 10 días, para captar correcciones del banco.
- **Sin duplicados:** el id es la referencia del banco o, si no la hay, un hash de cuenta + fecha + importe + comercio.
- **Pendientes:** se guardan y cuentan desde ya. Cuando el banco deja de devolverlos (porque se han contabilizado o anulado), se borran, y el contabilizado entra con su propio registro.
- **Límites:** los bancos suelen permitir unas 4 consultas automáticas al día por cuenta. Si se supera, Wally lo dice y no se rompe nada.

## Pruebas

```bash
pip install pytest && python -m pytest -q tests
```

Las pruebas no usan red: un banco falso imita las respuestas de la API.
