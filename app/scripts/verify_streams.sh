#!/usr/bin/env bash
# Verify camera streams (RTSP + HLS)
# Run from project root or via sudo

cd "$(dirname "$0")/../.." || exit 1

# Use virtualenv python
if [[ -d .venv ]]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi

$PYTHON app/scripts/verify_streams.py "$@"
