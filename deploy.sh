#!/usr/bin/env bash
set -euo pipefail

DIR="/home/slona/telegram_to_discord_bot"
SERVICE="telegram_to_discord_bot"
VENV_PIP="/home/slona/venv/bin/pip"

cd "$DIR"

echo "== Забираем изменения =="
git fetch origin
git reset --hard origin/master

echo "== Обновляем зависимости =="
"$VENV_PIP" install -r requirements.txt

echo "== Перезапускаем сервис =="
sudo systemctl restart "${SERVICE}.service"
sleep 2
sudo systemctl status "${SERVICE}.service" --no-pager

echo
echo "== Последние строки лога =="
journalctl -u "${SERVICE}" -n 10 --no-pager
