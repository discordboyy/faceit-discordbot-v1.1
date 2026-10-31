# Discord FACEIT бот (CS2)

## Команды
- `/link` — авторизация через Discord, бот берёт подключённый Steam и присылает в ЛС статистику FACEIT (уровень, ELO, матчи, Win Rate, K/D, хедшоты).
- `/faceit <ник>` — статистика по нику FACEIT без авторизации.

## Настройка
1. Discord Developer Portal → New Application.
   - Bot → Reset Token → `DISCORD_TOKEN`
   - OAuth2 → Client ID / Client Secret → `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`
   - OAuth2 → Redirects → добавь `https://your-domain.com/callback` (или `http://localhost:5000/callback` для локалки)
   - OAuth2 → URL Generator → scopes `bot` и `applications.commands`, право Send Messages → пригласи бота на сервер.
2. FACEIT: https://developers.faceit.com → App Studio → Create App → Server side API key → `FACEIT_API_KEY`.
3. Скопируй `.env.example` в `.env` и заполни.

## Запуск
```
python -m venv venv
venv\Scripts\activate        # Windows  (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python main.py
```

## Свой домен (HTTPS)
- A-запись домена → IP сервера (или Cloudflare Tunnel, если бот на домашнем ПК).
- Reverse proxy, например Caddy:
  ```
  your-domain.com {
      reverse_proxy 127.0.0.1:5000
  }
  ```
  и `WEB_HOST=127.0.0.1` в `.env`.
- Открой порты 80 и 443.

## Ограничения
- Connections доступны только через OAuth2 с согласия пользователя.
- Steam должен быть подключён в Discord и виден (публичный).
- Premier-рейтинга Valve в открытом API нет — используются данные FACEIT.
