#!/bin/sh
set -eu

umask 077

compose_file=${QT_PLATFORM_COMPOSE_FILE:-docker-compose.platform.yml}
env_file=${QT_PLATFORM_ENV_FILE:-.env.platform}
backup_dir=${QT_BACKUP_DIR:-${HOME:?HOME must be set}/qt-platform-backups}
archive_tmp=
checksum_tmp=
archive_final=
checksum_final=
complete=0

cleanup() {
    status=$?
    if [ "$complete" -ne 1 ]; then
        [ -z "$archive_tmp" ] || rm -f -- "$archive_tmp"
        [ -z "$checksum_tmp" ] || rm -f -- "$checksum_tmp"
        [ -z "$archive_final" ] || rm -f -- "$archive_final"
        [ -z "$checksum_final" ] || rm -f -- "$checksum_final"
    fi
    trap - EXIT HUP INT TERM
    exit "$status"
}

trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

compose() {
    docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

install -d -m 700 "$backup_dir"
chmod 700 "$backup_dir"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_final=$backup_dir/qt-platform-$timestamp.dump
checksum_final=$archive_final.sha256
if [ -e "$archive_final" ] || [ -e "$checksum_final" ]; then
    printf 'Backup already exists for timestamp %s\n' "$timestamp" >&2
    exit 1
fi

archive_tmp=$(mktemp "$backup_dir/.qt-platform-archive.XXXXXX.tmp")
checksum_tmp=$(mktemp "$backup_dir/.qt-platform-checksum.XXXXXX.tmp")
chmod 600 "$archive_tmp" "$checksum_tmp"

compose exec -T postgres sh -ec \
    'pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom' \
    > "$archive_tmp"
compose exec -T postgres sh -ec 'pg_restore --list >/dev/null' < "$archive_tmp"

digest=$(sha256sum "$archive_tmp")
digest=${digest%% *}
printf '%s  %s\n' "$digest" "$(basename "$archive_final")" > "$checksum_tmp"

mv "$archive_tmp" "$archive_final"
archive_tmp=
mv "$checksum_tmp" "$checksum_final"
checksum_tmp=
(cd "$backup_dir" && sha256sum --check "$(basename "$checksum_final")")

complete=1
printf 'Validated backup: %s\nChecksum: %s\n' "$archive_final" "$checksum_final"
