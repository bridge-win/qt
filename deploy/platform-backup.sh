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
archive_owned=0
checksum_owned=0
complete=0

remove_owned_publication() {
    temporary=$1
    published=$2
    owned=$3
    if [ "$owned" -eq 1 ] \
        && [ -n "$temporary" ] \
        && [ -n "$published" ] \
        && [ -e "$temporary" ] \
        && [ -e "$published" ] \
        && [ "$temporary" -ef "$published" ]; then
        rm -f -- "$published"
    fi
}

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$complete" -ne 1 ]; then
        remove_owned_publication "$checksum_tmp" "$checksum_final" "$checksum_owned"
        remove_owned_publication "$archive_tmp" "$archive_final" "$archive_owned"
    fi
    [ -z "$archive_tmp" ] || rm -f -- "$archive_tmp"
    [ -z "$checksum_tmp" ] || rm -f -- "$checksum_tmp"
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
archive_tmp=$(mktemp "$backup_dir/.qt-platform-archive.tmp.XXXXXX")
checksum_tmp=$(mktemp "$backup_dir/.qt-platform-checksum.tmp.XXXXXX")
chmod 600 "$archive_tmp" "$checksum_tmp"
publication_token=${archive_tmp##*/}
publication_token=${publication_token#.qt-platform-archive.tmp.}
publication_id=$timestamp-$$-$publication_token
archive_final=$backup_dir/qt-platform-$publication_id.dump
checksum_final=$archive_final.sha256

compose exec -T postgres sh -ec \
    'pg_dump --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom' \
    > "$archive_tmp"
compose exec -T postgres sh -ec 'pg_restore --list >/dev/null' < "$archive_tmp"

digest=$(sha256sum "$archive_tmp")
digest=${digest%% *}
printf '%s  %s\n' "$digest" "$(basename "$archive_final")" > "$checksum_tmp"

archive_owned=1
ln "$archive_tmp" "$archive_final"
checksum_owned=1
ln "$checksum_tmp" "$checksum_final"
(cd "$backup_dir" && sha256sum --check "$(basename "$checksum_final")")

complete=1
printf 'Validated backup: %s\nChecksum: %s\n' "$archive_final" "$checksum_final"
