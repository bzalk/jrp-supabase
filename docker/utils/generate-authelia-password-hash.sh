#!/usr/bin/env bash

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 '<password>'" >&2
  exit 1
fi

openssl passwd -6 "$1"
