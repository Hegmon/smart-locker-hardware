#!/bin/bash
#start.sh - Start all hardware services 
# Directory 
REPO_DIR="/home/pi/smart-locker-hardware"
cd $REPO_DIR || exit 

# Run update script 
bash scripts/update.sh
# Run hardware scripts 
python3 hardware/camera_stream_service.py

echo "Hardware services started at ${date +%s}"
