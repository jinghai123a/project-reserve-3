#!/bin/sh
set -eu
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) cat "${CLOUDKIT_GITHUB_TOKEN_FILE:?}" ;;
  *) exit 1 ;;
esac
