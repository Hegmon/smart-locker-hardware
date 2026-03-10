#!/bin/bash
# update.sh - Pull latest code and update environment
# Directory of Repo
REPO_DIR="/home/pi/smart-locker-hardware"
cd $REPO_DIR || exit

# Pull latest code 
git pull origin main 

# Install python dependencies
pip3 install -r requirements.txt

# Restart docker container if docker-compose exits
if [-f "$REPO_DIR/docker-compose.yml"]; then 
   docker compose down
   docker compose up -d 
fi 
echo "Update completed at ${date +%s}
