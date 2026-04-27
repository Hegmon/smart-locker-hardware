#!/usr/bin/env bash
set -euo pipefail

nmcli connection down SmartLockerHotspot || true
