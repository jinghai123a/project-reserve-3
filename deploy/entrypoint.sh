#!/bin/sh
set -eu
umask 077
export GIT_ASKPASS=/opt/cloudkit/deploy/git-askpass.sh
# Keep the token out of the image, process argv, clone URLs and Git config.
if [ -r "${CLOUDKIT_GITHUB_TOKEN_FILE:-/run/secrets/github_token}" ]; then
  GITHUB_TOKEN=$(cat "${CLOUDKIT_GITHUB_TOKEN_FILE:-/run/secrets/github_token}")
  export GITHUB_TOKEN
fi
case "${1:-worker}" in
  worker|prepare|probe|health|ready)
    exec python /opt/cloudkit/deploy/runtime.py "$@" ;;
  *) exec "$@" ;;
esac
