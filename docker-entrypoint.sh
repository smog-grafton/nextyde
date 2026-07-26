#!/bin/sh
set -eu

encrypted_session="${TELEGRAM_SESSION_ENCRYPTED_FILE:-/app/deploy/narabox_telebot.session.enc}"
session_name="${TG_SESSION_NAME:-narabox_data_pipe}"

case "$session_name" in
    *.session) session_path="$session_name" ;;
    *) session_path="${session_name}.session" ;;
esac

if [ -n "${TELEGRAM_SESSION_KEY:-}" ] && [ -f "$encrypted_session" ]; then
    session_dir="$(dirname "$session_path")"
    restore_path="${session_path}.restore.$$"

    mkdir -p "$session_dir"
    openssl enc -d \
        -aes-256-cbc \
        -pbkdf2 \
        -iter 200000 \
        -in "$encrypted_session" \
        -out "$restore_path" \
        -pass env:TELEGRAM_SESSION_KEY
    chmod 600 "$restore_path"
    mv -f "$restore_path" "$session_path"
    rm -f "${session_path}-journal"
    echo "Restored encrypted Telegram session at ${session_path}"
elif [ -f "$encrypted_session" ] && [ ! -f "$session_path" ]; then
    echo "Encrypted Telegram session found, but TELEGRAM_SESSION_KEY is not configured." >&2
fi

exec "$@"
