#!/bin/bash
# App Service startup script for Mandali Teams Relay
# Dependencies are installed during deployment by Oryx build

cd /home/site/wwwroot

echo "Starting Uvicorn server..."
exec gunicorn app:app --bind 0.0.0.0:8000 --worker-class uvicorn.workers.UvicornWorker --timeout 120
