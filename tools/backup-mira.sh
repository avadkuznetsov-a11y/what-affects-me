#!/usr/bin/env bash
# Шифрованный бэкап базы Миры. Шаблон: на сервере кладётся в каталог бэкапов
# и запускается cron раз в сутки (50 3 * * *) и руками перед каждой выкладкой.
#
# Что делает:
#   1. снимает копию базы на ходу (sqlite3 .backup - сервис не останавливаем,
#      копия согласованная, даже если в эту секунду кто-то пишет);
#   2. пакует её в tar.gz и шифрует AES-256-CBC, ключ выводится через pbkdf2;
#   3. тут же расшифровывает обратно и проверяет PRAGMA integrity_check -
#      бэкап, который не открывается, хуже, чем никакого: о нём узнают в день,
#      когда он понадобится;
#   4. хранит 14 дней (столько обещано в политике: из бэкапов данные удалённого
#      человека уходят через 14 дней);
#   5. отправляет копию по sftp на второй сервер, в backups/mira/.
#
# Секретов в файле нет. Ключ шифрования - в файле только для root (600),
# адрес второго сервера - в переменной окружения, её задаёт строка cron:
#   MIRA_BACKUP_REMOTE=пользователь@адрес /путь/backup-mira.sh
# Без ключа бэкап не прочесть никому, включая нас: ключ хранить отдельно от
# сервера (в менеджере паролей владельца).
#
# Восстановление:
#   openssl enc -d -aes-256-cbc -pbkdf2 -pass file:/root/.mira-backup.key \
#       -in mira-ГГГГММДД-ЧЧММСС.tar.gz.enc | tar -xzf -
set -euo pipefail
umask 077

APP_DIR="${MIRA_DIR:-/opt/mira}"
DB="${MIRA_BACKUP_DB:-$APP_DIR/data/mira.db}"
OUT_DIR="${MIRA_BACKUP_DIR:-/opt/backups/mira}"
KEY_FILE="${MIRA_BACKUP_KEY:-/root/.mira-backup.key}"
SSH_KEY="${MIRA_BACKUP_SSH_KEY:-/root/.ssh/webhost1_backup}"
REMOTE="${MIRA_BACKUP_REMOTE:-}"
REMOTE_DIR="backups/mira"
KEEP_DAYS=14

say() { echo "$(date '+%F %T') backup-mira: $*"; }
die() { say "ОШИБКА: $*" >&2; exit 1; }

[ -f "$DB" ] || die "нет базы $DB"
[ -s "$KEY_FILE" ] || die "нет ключа шифрования $KEY_FILE"
mkdir -p "$OUT_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
name="mira-$stamp.tar.gz.enc"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# 1. Копия на ходу. Через python, а не через утилиту sqlite3: python3 на
# сервере есть всегда, а sqlite3 надо ставить отдельно.
python3 - "$DB" "$work/mira.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY

# 2. Упаковать и зашифровать
tar -C "$work" -czf "$work/mira.tar.gz" mira.db
openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$KEY_FILE" \
    -in "$work/mira.tar.gz" -out "$OUT_DIR/$name.part"

# 3. Проверка: расшифровать, распаковать, целостность базы
mkdir "$work/check"
openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$KEY_FILE" -in "$OUT_DIR/$name.part" \
    | tar -xzf - -C "$work/check"
verdict="$(python3 - "$work/check/mira.db" <<'PY'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(db.execute("PRAGMA integrity_check").fetchone()[0])
PY
)"
if [ "$verdict" != "ok" ]; then
    rm -f "$OUT_DIR/$name.part"
    die "копия не прошла integrity_check: $verdict"
fi
# Готовый файл появляется под своим именем только после проверки: недописанный
# или битый бэкап не должен выглядеть как настоящий.
mv "$OUT_DIR/$name.part" "$OUT_DIR/$name"
say "готово: $OUT_DIR/$name ($(du -h "$OUT_DIR/$name" | cut -f1))"

# 4. Срок хранения
find "$OUT_DIR" -maxdepth 1 -name 'mira-*.tar.gz.enc' -mtime +$((KEEP_DAYS - 1)) -delete
find "$OUT_DIR" -maxdepth 1 -name 'mira-*.part' -mmin +60 -delete

# 5. Копия на второй сервер
if [ -z "$REMOTE" ]; then
    die "MIRA_BACKUP_REMOTE не задан - копия только на этом сервере"
fi
sftp -i "$SSH_KEY" -o BatchMode=yes -b - "$REMOTE" <<SFTP
-mkdir backups
-mkdir $REMOTE_DIR
put $OUT_DIR/$name $REMOTE_DIR/$name
SFTP
say "копия отправлена в $REMOTE_DIR/"
