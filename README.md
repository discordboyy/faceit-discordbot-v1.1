# Discord FACEIT бот (CS2) + SQLite

## Команды
- `/link` — авторизация через Discord, бот берёт подключённый Steam, запоминает его и присылает статистику FACEIT (в ЛС и на странице после авторизации).
- `/stats` — своя статистика без повторной авторизации.
- `/faceit <ник>` — статистика по нику FACEIT.
- `/unlink` — удалить свои данные из базы.

## Что хранится в SQLite (bot.db)
- `users` — discord_id, steam_id64, steam_name, время обновления (OAuth-токены не хранятся)
- `oauth_states` — одноразовые state для ссылок авторизации (живут 10 минут)
- `cache` — кэш ответов FACEIT (по умолчанию 5 минут)

## Discord Developer Portal
- Bot → Reset Token → `DISCORD_TOKEN`
- OAuth2 → Client ID / Client Secret → `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`
- OAuth2 → Redirects → `https://makki-solutions.bid/callback`
- OAuth2 → URL Generator → scopes `bot` + `applications.commands` (+ права Send Messages, Embed Links) → открой ссылку и добавь бота на сервер

## FACEIT
https://developers.faceit.com → App Studio → Create App → Server side API key → `FACEIT_API_KEY`

## Установка на сервер (Ubuntu)
```
mkdir -p ~/faceit-bot && cd ~/faceit-bot
# скопируй сюда файлы проекта
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
```

### systemd — /etc/systemd/system/faceit-bot.service
```
[Unit]
Description=Discord FACEIT bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/faceit-bot
ExecStart=/home/ubuntu/faceit-bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload
sudo systemctl enable --now faceit-bot
journalctl -u faceit-bot -f
```

### Reverse proxy для /callback
Сначала проверь, что уже занимает 80/443:
```
sudo ss -tlnp | grep -E ':80 |:443 |:5000 '
```
**Если nginx** — в server-блок домена добавь:
```
location /callback {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
затем `sudo nginx -t && sudo systemctl reload nginx`.

**Если Caddy** — в блок домена добавь:
```
handle /callback* {
    reverse_proxy 127.0.0.1:5000
}
```
затем `sudo systemctl reload caddy`.

Проверка: открой `https://makki-solutions.bid/callback` — должно появиться «Некорректный запрос».

## Ограничения
- Connections доступны только через OAuth2 с согласия пользователя.
- Steam должен быть подключён в Discord и виден (публичный).
- Premier-рейтинга Valve в открытом API нет — используются данные FACEIT.
