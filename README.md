# Telegram-To-Discord

Пересылает сообщения из Telegram-канала(ов) в Discord через webhook: текст с сохранением ссылок и форматирования, фото/видео (включая альбомы), с фоллбеком на ссылку на пост при превышении лимита размера вложения.

Основано на архивном проекте [UrekD/Telegram-To-Discord](https://github.com/UrekD/Telegram-To-Discord), переписано и доработано.

## Требования

- Python 3.11+
- Discord webhook
- Telegram API токены (APPID/APIHASH)

## Установка

```bash
pip install -r requirements.txt
cp .env.example .env
# Заполнить .env своими значениями
mkdir -p temp  # или другой путь, указанный в DLLOC — папка должна существовать до старта
python3 main.py
```

При первом запуске (или если `.session`-файл протух) Telegram запросит номер телефона и код подтверждения — нужен интерактивный терминал. После успешного входа создаётся `.session`-файл, и дальнейшие запуски (в том числе под systemd) проходят без интерактива.

APPID и APIHASH создаются здесь: https://core.telegram.org/api/obtaining_api_id

## Деплой

В проде бот запускается через systemd-юнит `telegram_to_discord_bot.service`.

## Лицензия

[MIT](LICENSE)
