#!/usr/bin/env bash
# Run Frija from this directory without installing it.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m frija "$@"
