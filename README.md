# Discord FACEIT бот (CS2)

## Команды
- `/link` — авторизация через Discord, бот берёт подключённый Steam, запоминает его и присылает статистику FACEIT (в ЛС и на странице после авторизации).
- `/stats` — своя статистика без повторной авторизации.
- `/faceit <ник>` — статистика по нику FACEIT.
- `/unlink` — удалить свои данные из базы.

Команды ограничены: не чаще одного запуска раз в 15 секунд на пользователя.

## Что хранится (SQLite)
- `users` — discord_id, steam_id64, steam_name (если задан `DB_ENCRYPTION_KEY`, SteamID и имя шифруются Fernet)
- `oauth_states` — одноразовые state для ссылок авторизации (живут 10 минут)
- `cache` — кэш ответов FACEIT (по умолчанию 5 минут); ключи кэша хранятся как HMAC, по ним нельзя узнать SteamID
- OAuth-токены Discord не хранятся: токен используется один раз и сразу отзывается

## Защита OAuth
- state одноразовый и привязан к тому, кто вызвал `/link`.
- После авторизации бот сверяет id аккаунта Discord (`/users/@me`) с владельцем ссылки. Если ссылку открыл другой человек, привязка не выполняется.

## Discord Developer Portal
- Bot → Reset Token → `DISCORD_TOKEN`
- OAuth2 → Client ID / Client Secret → `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`
- OAuth2 → Redirects → `https://faceit.makki-solutions.bid/callback` (должен совпадать с `.env` символ в символ) → Save Changes
- Приглашение бота (scopes `bot` + `applications.commands`, права Send Messages + Embed Links):
  `https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&permissions=18432&integration_type=0&scope=bot+applications.commands`

## FACEIT
https://developers.faceit.com → App Studio → Create App → Server side API key → `FACEIT_API_KEY`

## Ключ шифрования базы
```
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Сохрани ключ в менеджере паролей. Без него зашифрованные записи не расшифровать (пользователям придётся сделать `/link` заново).

## Развёртывание на сервере (Ubuntu + nginx)

Схема: код в `/opt/faceit-bot` (только для чтения), база в `/var/lib/faceit-bot`, секреты в зашифрованном виде в `/etc/faceit-bot/env.cred`, сервис работает под отдельным пользователем `faceitbot` с ограничениями systemd.

```
sudo useradd --system --no-create-home --shell /usr/sbin/nologin faceitbot
sudo git clone https://github.com/discordboyy/faceit-discordbot-v1.1.git /opt/faceit-bot
cd /opt/faceit-bot
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
```

Секреты (файл `/tmp/faceit.env` с переменными из `.env.example`, `DB_PATH=/var/lib/faceit-bot/bot.db`) шифруются привязанным к серверу ключом systemd:
```
sudo mkdir -p /etc/faceit-bot && sudo chmod 700 /etc/faceit-bot
sudo systemd-creds encrypt --name=env /tmp/faceit.env /etc/faceit-bot/env.cred
shred -u /tmp/faceit.env
```
Посмотреть/изменить: `sudo systemd-creds decrypt --name=env /etc/faceit-bot/env.cred -` (расшифровать → поправить → зашифровать заново).

### /etc/systemd/system/faceit-bot.service
```
[Unit]
Description=Discord FACEIT bot
After=network-online.target
Wants=network-online.target

[Service]
User=faceitbot
Group=faceitbot
WorkingDirectory=/opt/faceit-bot
ExecStart=/opt/faceit-bot/venv/bin/python main.py
LoadCredentialEncrypted=env:/etc/faceit-bot/env.cred
StateDirectory=faceit-bot
Restart=always
RestartSec=5
UMask=0077

NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
CapabilityBoundingSet=
MemoryMax=300M

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload
sudo systemctl enable --now faceit-bot
sudo journalctl -u faceit-bot -n 30 --no-pager
sudo systemd-analyze security faceit-bot
```

### nginx (поддомен только под /callback)
```
server {
    listen 80;
    server_name faceit.makki-solutions.bid;

    location = /callback {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location / { return 404; }
}
```
Сертификат: `sudo certbot --nginx -d faceit.makki-solutions.bid`.

## Сон бота ночью (01:00–05:00 UTC+1 = 00:00–04:00 UTC)
Два таймера systemd: один останавливает сервис, другой запускает.
```
sudo tee /etc/systemd/system/faceit-bot-sleep.service > /dev/null << 'EOT'
[Unit]
Description=Stop FACEIT bot for the night
[Service]
Type=oneshot
ExecStart=/bin/systemctl stop faceit-bot.service
EOT

sudo tee /etc/systemd/system/faceit-bot-sleep.timer > /dev/null << 'EOT'
[Unit]
Description=Night sleep for FACEIT bot
[Timer]
OnCalendar=*-*-* 00:00:00 UTC
[Install]
WantedBy=timers.target
EOT

sudo tee /etc/systemd/system/faceit-bot-wake.service > /dev/null << 'EOT'
[Unit]
Description=Start FACEIT bot in the morning
[Service]
Type=oneshot
ExecStart=/bin/systemctl start faceit-bot.service
EOT

sudo tee /etc/systemd/system/faceit-bot-wake.timer > /dev/null << 'EOT'
[Unit]
Description=Morning wake for FACEIT bot
[Timer]
OnCalendar=*-*-* 04:00:00 UTC
[Install]
WantedBy=timers.target
EOT

sudo systemctl daemon-reload
sudo systemctl enable --now faceit-bot-sleep.timer faceit-bot-wake.timer
systemctl list-timers | grep faceit
```
Если нужно следовать местному времени Осло (с переходом на летнее), замени `00:00:00 UTC` на `01:00:00 Europe/Oslo` и `04:00:00 UTC` на `05:00:00 Europe/Oslo`.

## Ограничения
- Connections доступны только через OAuth2 с согласия пользователя.
- Steam должен быть подключён в Discord и виден (публичный).
- Premier-рейтинга Valve в открытом API нет — используются данные FACEIT.
