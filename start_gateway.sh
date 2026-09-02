#!/bin/bash
echo "Starting Gateway Proxy on 0.0.0.0:8080..."
echo "(Make sure you have exported DP_BUS_BASE_URL to point to your remote VM if it's hosted elsewhere!)"
.venv/bin/python3 .venv/bin/uvicorn gateway.app:app --host 0.0.0.0 --port 8080
