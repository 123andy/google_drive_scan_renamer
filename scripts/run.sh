#!/usr/bin/env bash
# Convenience wrapper. Runs the host-side launcher, which stages inbox scans into a
# local scratch dir, runs the Dockerized processor over it, then moves results back
# to PROCESSED and drains the inbox. (We can't bind-mount Google Drive into Docker.)
#
# Usage:
#   ./run.sh                # process the inbox
#   DRY_RUN=true ./run.sh   # OCR + analyze + log only, move/delete nothing
set -euo pipefail
cd "$(dirname "$0")"
exec python3 stage_and_run.py
