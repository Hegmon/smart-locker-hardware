#!/usr/bin/env bash
set -euo pipefail

nmcli radio wifi on
nmcli connection up SmartLockerHotspot
