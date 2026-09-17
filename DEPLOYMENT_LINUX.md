# Nasazení aplikace Asistent pedagoga na školní Linux server

Tento dokument je určen správci serveru. Cílová veřejná adresa aplikace je:

```text
https://asistent.arcig.cz
```

Aplikace je napsaná pro Python 3.11 nebo novější, nepoužívá externí Python
balíčky a ukládá provozní data do SQLite. Pythonový proces poslouchá pouze na
lokální adrese `127.0.0.1:8000`; HTTPS ukončuje existující školní reverzní
proxy nebo Caddy.

## 1. Požadavky

- Linux se systemd;
- Python 3.11 nebo novější;
- Git a nástroj `sqlite3` pro zálohy;
- DNS záznam `asistent.arcig.cz` směřující na školní proxy/server;
- příchozí HTTPS na portu 443 (pro automatické ACME certifikáty také port 80);
- odchozí HTTPS na `arcig.edupage.org:443`;
- odchozí spojení na SMTP server školy;
- samostatná schránka nebo SMTP účet pro odesílatele souhrnů.

Příklad instalace nástrojů na Debianu/Ubuntu:

```bash
sudo apt update
sudo apt install -y git python3 sqlite3
python3 --version
```

Na RHEL/AlmaLinux/Rocky Linuxu je potřeba použít odpovídající balíčky přes
`dnf`.

## 2. DNS a síť

V DNS zřídit:

```text
asistent.arcig.cz.  A  <veřejná IPv4 adresa proxy/serveru>
```

Záznam AAAA přidávat pouze v případě, že je IPv6 skutečně funkční a směrované.
Ve firewallu nemá být veřejně dostupný port 8000. Z internetu mají být
dostupné pouze porty reverzní proxy, typicky 80 a 443.

## 3. Systémový účet, zdrojový kód a datový adresář

```bash
sudo useradd --system --home-dir /var/lib/rozvrh-asistent --shell /usr/sbin/nologin rozvrh
sudo git clone --branch main https://github.com/bohata-droid/rozvrh-asistent.git /opt/rozvrh-asistent
sudo chown -R root:rozvrh /opt/rozvrh-asistent
sudo chmod -R g=rX,o= /opt/rozvrh-asistent
sudo install -d -o rozvrh -g rozvrh -m 0750 /var/lib/rozvrh-asistent
```

Pokud účet `rozvrh` již existuje, příkaz `useradd` se vynechá. Zdrojový kód je
záměrně vlastněn uživatelem `root`; běžící aplikace smí zapisovat pouze do
`/var/lib/rozvrh-asistent`.

Kontrola syntaxe:

```bash
sudo python3 -m py_compile /opt/rozvrh-asistent/fetch_timetable.py /opt/rozvrh-asistent/server.py
```

## 4. Provozní konfigurace a tajné údaje

Vytvořit soubor `/etc/rozvrh-asistent.env`:

```ini
ASSISTANT_HOST=127.0.0.1
ASSISTANT_PORT=8000
ASSISTANT_DB_PATH=/var/lib/rozvrh-asistent/assistant.db
ASSISTANT_ADMIN_PASSWORD="NAHRADIT_DLOUHYM_NAHODNYM_HESLEM"

SMTP_HOST="NAHRADIT_SMTP_SERVEREM_SKOLY"
SMTP_PORT=587
SMTP_USER="NAHRADIT_SMTP_UZIVATELEM"
SMTP_PASSWORD="NAHRADIT_SMTP_HESLEM"
SMTP_FROM="asistent@arcig.cz"
SMTP_TLS=1
SMTP_SSL=0
```

Pro SMTP s přímým TLS na portu 465 nastavit místo posledních tří relevantních
řádků:

```ini
SMTP_PORT=465
SMTP_TLS=0
SMTP_SSL=1
```

Vygenerování hesla správce například:

```bash
openssl rand -base64 36
```

Oprávnění konfiguračního souboru:

```bash
sudo chown root:rozvrh /etc/rozvrh-asistent.env
sudo chmod 0640 /etc/rozvrh-asistent.env
```

Soubor s tajnými údaji se nesmí ukládat do Git repozitáře ani posílat
e-mailem v otevřené podobě.

## 5. Služba systemd

Vytvořit `/etc/systemd/system/rozvrh-asistent.service`:

```ini
[Unit]
Description=Rozvrh asistenta pedagoga
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rozvrh
Group=rozvrh
WorkingDirectory=/opt/rozvrh-asistent
EnvironmentFile=/etc/rozvrh-asistent.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 /opt/rozvrh-asistent/server.py
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/rozvrh-asistent

[Install]
WantedBy=multi-user.target
```

Službu aktivovat:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rozvrh-asistent
sudo systemctl status rozvrh-asistent
```

Lokální kontrola:

```bash
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
```

Očekávaná odpověď:

```json
{"ok": true}
```

Log aplikace:

```bash
sudo journalctl -u rozvrh-asistent -f
```

## 6. HTTPS reverzní proxy

### Varianta A: existující školní Nginx

Do konfigurace virtuálních serverů doplnit ekvivalent:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name asistent.arcig.cz;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name asistent.arcig.cz;

    ssl_certificate     /CESTA/K/CERTIFIKATU/fullchain.pem;
    ssl_certificate_key /CESTA/K/PRIVATNIMU_KLICI/privkey.pem;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;
    add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;
}
```

Certifikát získat a obnovovat postupem, který škola používá pro ostatní
subdomény. Poté ověřit a načíst konfiguraci:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

### Varianta B: Caddy

Pokud server nemá existující reverzní proxy, lze nainstalovat Caddy z jeho
oficiálního repozitáře a do `/etc/caddy/Caddyfile` vložit:

```caddy
asistent.arcig.cz {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000

    header {
        X-Content-Type-Options nosniff
        Referrer-Policy same-origin
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
    }
}
```

Poté:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy při správném DNS a dostupných portech 80/443 automaticky získá a
obnovuje veřejně důvěryhodný TLS certifikát.

## 7. Kontrola po nasazení

```bash
curl --fail --silent --show-error https://asistent.arcig.cz/api/health
```

V prohlížeči následně ověřit:

1. načtení aktuálního i následujícího týdne;
2. přihlášení do nastavení heslem `ASSISTANT_ADMIN_PASSWORD`;
3. vytvoření zapisujícího uživatele s PINem;
4. označení skupiny a odškrtnutí hodiny celé třídy;
5. uložení poznámky;
6. opětovné načtení stránky a zachování změn;
7. ruční odeslání e-mailového souhrnu v nastavení.

Automatický e-mailový souhrn se odesílá v 16:00 Europe/Prague, pokud existují
neodeslané změny a alespoň jeden aktivní příjemce s e-mailovou adresou. Proces
proto musí běžet nepřetržitě.

## 8. Zálohování

Vytvořit adresář přístupný pouze správci a službě:

```bash
sudo install -d -o rozvrh -g rozvrh -m 0750 /var/backups/rozvrh-asistent
```

Konzistentní zálohu za běhu vytvoří SQLite příkaz `.backup`:

```bash
stamp=$(date +%F-%H%M)
sudo -u rozvrh sqlite3 /var/lib/rozvrh-asistent/assistant.db \
  ".backup '/var/backups/rozvrh-asistent/assistant-${stamp}.db'"
```

Doporučení: spouštět denně po 17:00, uchovávat alespoň 90 dní a kopírovat také
do běžného školního zálohovacího systému. Nekopírovat pouze živý soubor
`assistant.db` bez SQLite backupu, protože databáze používá WAL.

Obnova:

1. zastavit `rozvrh-asistent.service`;
2. zazálohovat aktuální databázi;
3. nahradit `/var/lib/rozvrh-asistent/assistant.db` ověřenou zálohou;
4. nastavit vlastníka `rozvrh:rozvrh` a režim `0600`;
5. znovu spustit službu a ověřit `/api/health`.

## 9. Aktualizace aplikace

Před aktualizací provést zálohu databáze. Poté:

```bash
sudo git -C /opt/rozvrh-asistent pull --ff-only origin main
sudo python3 -m py_compile /opt/rozvrh-asistent/fetch_timetable.py /opt/rozvrh-asistent/server.py
sudo systemctl restart rozvrh-asistent
sudo systemctl status rozvrh-asistent
curl --fail --silent --show-error https://asistent.arcig.cz/api/health
```

Databázové tabulky se při startu vytvářejí a doplňují automaticky. Restart
zruší pouze přihlášené webové relace; uživatelé se znovu přihlásí PINem.

## 10. Bezpečnostní poznámky

- Veřejně nevystavovat port 8000, pouze HTTPS reverzní proxy.
- Použít unikátní dlouhé heslo správce a nesdílet je s běžnými uživateli.
- Databáze a zálohy obsahují jména a e-mailové adresy; přístup omezit na
  správce a účet `rozvrh`.
- Pokud rozvrh nemá být veřejně čitelný, omezit přístup na školní síť/VPN nebo
  přidat autentizaci na reverzní proxy. Zápis změn je v samotné aplikaci
  chráněn PINem, ale zobrazení rozvrhu je úmyslně veřejné.
- Pravidelně kontrolovat `journalctl`, funkčnost záloh a úspěšnost e-mailových
  souhrnů.

