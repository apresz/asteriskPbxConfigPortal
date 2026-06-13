#!/bin/sh
set -eu

asterisk_uid="${ASTERISK_UID:-1000}"
asterisk_gid="${ASTERISK_GID:-1000}"

if getent group asterisk >/dev/null 2>&1; then
  asterisk_group="asterisk"
elif getent group "$asterisk_gid" >/dev/null 2>&1; then
  asterisk_group="$(getent group "$asterisk_gid" | cut -d: -f1)"
else
  groupadd -g "$asterisk_gid" asterisk
  asterisk_group="asterisk"
fi

if ! id -u asterisk >/dev/null 2>&1; then
  useradd --system --uid "$asterisk_uid" --gid "$asterisk_group" --home-dir /var/lib/asterisk --shell /usr/sbin/nologin asterisk
fi

for path in /etc/asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk; do
  mkdir -p "$path"
  chown asterisk:"$asterisk_group" "$path"
done

exec "$@"
