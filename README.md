# Asistent pedagoga – týdenní rozvrh

Aplikace načte týdenní rozvrh třídy 2.A z veřejného EduPage, umožní pověřeným
uživatelům vybrat jednu dělenou skupinu v každé hodině a zapsat společnou
poznámku. Změny ukládá do SQLite a v 16:00 z nich může odeslat e-mailový souhrn.

Postup pro ostré nasazení na školní Linux server pod doménou
`asistent.arcig.cz` je v souboru [DEPLOYMENT_LINUX.md](DEPLOYMENT_LINUX.md).

## Spuštění

Je potřeba Python 3.11 nebo novější. Aplikace nepoužívá žádné externí balíčky.

```powershell
$env:ASSISTANT_ADMIN_PASSWORD = "zvolte-silne-heslo"
python server.py
```

Potom otevřete `http://127.0.0.1:8000`. Při prvním spuštění otevřete ozubené
kolečko, zadejte heslo správce a přidejte uživatele. Pokud proměnnou s heslem
nenastavíte, dočasné výchozí heslo je `zmenit-me`.

Při otevření samotného `index.html` nebo verze na GitHub Pages se automaticky
zobrazí poslední exportovaný týden v režimu pouze pro čtení. Přihlašování,
poznámky, ukládání výběru a e-maily vyžadují spuštěný `server.py`.

Databáze vznikne jako `assistant.db`. Pro provoz na jiném serveru lze cestu,
adresu a port změnit proměnnými `ASSISTANT_DB_PATH`, `ASSISTANT_HOST` a
`ASSISTANT_PORT`. Server musí běžet nepřetržitě, aby mohl v 16:00 odesílat
souhrny; samotný GitHub Pages pro ukládání dat a e-mail nestačí.

## E-mailové souhrny

Nastavte údaje SMTP v prostředí, ve kterém běží server:

```powershell
$env:SMTP_HOST = "smtp.example.cz"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "uzivatel@example.cz"
$env:SMTP_PASSWORD = "heslo"
$env:SMTP_FROM = "asistent@example.cz"
$env:SMTP_TLS = "1"
python server.py
```

Pro SMTP přes SSL nastavte `SMTP_SSL=1` (obvykle port 465). Příjemci se určují
v nastavení aplikace volbou „Dostává denní souhrn“. Neodeslané změny lze ze
stejného místa poslat i ručně.

## Aktualizace statického exportu

Příkaz níže stáhne týden obsahující zadaný den do `timetable.json` a
`timetable-data.js`; používá ho také stávající GitHub Actions workflow.

```powershell
python fetch_timetable.py --date 2026-09-17
```

Server si jednotlivé navštívené týdny ukládá na hodinu do mezipaměti. Tlačítko
„Aktualizovat“ v aplikaci vynutí nové načtení z EduPage.

## Nasazení na Render

Repozitář obsahuje `render.yaml`, takže nejjednodušší ostré nasazení je přes
Render Blueprint:

1. Commitněte a pushněte všechny změny do GitHub repozitáře.
2. Na Renderu zvolte **New → Blueprint** a připojte repozitář
   `bohata-droid/rozvrh-asistent`.
3. Render načte `render.yaml`. Při vytváření zadejte vlastní silné
   `ASSISTANT_ADMIN_PASSWORD`.
4. Po nasazení otevřete přidělenou adresu `https://…onrender.com`.
5. V aplikaci otevřete nastavení, přidejte zapisujícího asistenta a příjemce
   souhrnů.

Konfigurace používá placený webový proces a 1GB trvalý disk připojený jako
`/var/data`; databáze je uložená v `/var/data/assistant.db`. Bez trvalého disku
by se SQLite databáze při restartu nebo novém nasazení ztratila. Bezplatný
Render není pro ostrou verzi vhodný: uspává proces, nepodporuje trvalý disk a
blokuje běžné SMTP porty.

Pro e-mail přidejte v Render Dashboardu na stránce služby v části
**Environment** tyto tajné proměnné a službu znovu nasaďte:

- `SMTP_HOST`
- `SMTP_PORT` (obvykle `587`)
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_TLS=1`

Pokud poskytovatel vyžaduje přímé SSL na portu 465, nastavte místo toho
`SMTP_PORT=465`, `SMTP_SSL=1` a `SMTP_TLS=0`. Health check je dostupný na
`/api/health`. Render po každém pushi do propojené větve aplikaci znovu nasadí.
