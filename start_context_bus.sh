#!/bin/bash
echo "Starting Context Bus Backend via Docker (Live on port 8000)..."
cd store
docker compose up --build -d
cd ..

echo "Starting Admin Dashboard on port 3001..."
cd admin-dashboard
npm install
npm run dev &
DASH_PID=$!

echo "Context Bus and Admin Dashboard are running!"
trap "kill $DASH_PID" EXIT
wait
