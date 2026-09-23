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

# The console Restart control talks to the Docker Engine over a Unix socket.
# The mount is optional; when it is present, grant the runtime user the
# socket's group so connect() succeeds after the privilege drop. --groups
# replaces --init-groups here because app is not in that group in /etc/group.
# Supplementary groups the service still needs after the drop.
#
# Read this before changing it. `setpriv --groups` REPLACES the whole list,
# and `--init-groups` builds it from /etc/group, where these host gids do not
# appear. Either way, a group granted to the container from outside - compose
# `group_add`, docker `--group-add` - is gone the moment privileges drop
# unless it is named here. So each one is looked up from the device or socket
# itself rather than assumed.
EXTRA_GROUPS="${PGID}"

DOCKER_SOCKET="${DOCKER_SOCKET:-/var/run/docker.sock}"
if [ -S "$DOCKER_SOCKET" ]; then
  EXTRA_GROUPS="${EXTRA_GROUPS},$(stat -c %g "$DOCKER_SOCKET")"
fi

# The multi-view composite opens the iGPU's render node. Losing its group
# surfaces as ffmpeg's thoroughly misleading "No VA display found", not as a
# permission error, so this is worth the two lines.
RENDER_NODE="${MULTIVIEW_RENDER_NODE:-/dev/dri/renderD128}"
if [ -e "$RENDER_NODE" ]; then
  EXTRA_GROUPS="${EXTRA_GROUPS},$(stat -c %g "$RENDER_NODE")"
fi

if [ "$EXTRA_GROUPS" = "${PGID}" ]; then
  exec setpriv --reuid="${PUID}" --regid="${PGID}" --init-groups "$@"
fi
exec setpriv --reuid="${PUID}" --regid="${PGID}" --groups="${EXTRA_GROUPS}" "$@"
