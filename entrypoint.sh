#!/bin/sh
# Runs as root only long enough to own /data, then drops to PUID:PGID.
# tini is PID 1 above this script, so signals still reach the service.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Already unprivileged (compose `user:`), or root was asked for explicitly:
# there is nothing to drop and nothing we could chown anyway.
if [ "$(id -u)" != "0" ] || [ "$PUID" = "0" ]; then
  exec "$@"
fi

# Playwright's driver looks the running uid up in passwd, so the entries must
# exist. groupadd and getent are not guaranteed in a slim image, so append
# directly when the id is not already present.
grep -q "^[^:]*:[^:]*:${PGID}:" /etc/group  || echo "app:x:${PGID}:" >> /etc/group
grep -q "^[^:]*:[^:]*:${PUID}:" /etc/passwd || \
  echo "app:x:${PUID}:${PGID}::/tmp:/usr/sbin/nologin" >> /etc/passwd

# The roster and caches live here and the service writes them. Only recurse
# when the mount's own ownership is wrong, so a correctly owned volume costs
# one stat on every start rather than a walk.
mkdir -p /data
if [ "$(stat -c %u:%g /data)" != "${PUID}:${PGID}" ]; then
  chown -R "${PUID}:${PGID}" /data
fi

export HOME=/tmp
exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups "$@"
