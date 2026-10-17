#!/bin/bash
cd "$(dirname "$0")"
pip3 install -q fastapi uvicorn requests
(sleep 2; open http://localhost:8000 2>/dev/null || xdg-open http://localhost:8000) &
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
