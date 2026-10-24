#!/bin/sh
# Convenience launcher:  ./pintester.sh dump
cd "$(dirname "$0")" && exec python3 -m pintester "$@"
