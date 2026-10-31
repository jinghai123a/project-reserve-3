#!/bin/sh
set -eu
umask 077
# Only process memory receives the key. Compose metadata stores the file path.
if [ -s /run/secrets/tailscale_authkey ]; then
  TS_AUTHKEY=$(cat /run/secrets/tailscale_authkey)
  export TS_AUTHKEY
fi
# Empty key + no saved login remains unauthenticated; health must not pass.
exec /usr/local/bin/containerboot
