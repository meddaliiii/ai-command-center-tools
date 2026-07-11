#!/bin/bash
# يشغّل جسر Node.js (Puter.js) في الخلفية، ثم سيرفر Python الرئيسي في المقدمة.
set -e

echo "[start.sh] بدء تشغيل جسر Puter (Node.js)..."
node /app/bridge/server.js &
BRIDGE_PID=$!

# مهلة قصيرة للتأكد من إقلاع الجسر قبل بدء Python
sleep 2

echo "[start.sh] بدء تشغيل سيرفر الأدوات (Python)..."
python server.py

# إذا خرج Python لأي سبب، أوقف الجسر أيضاً
kill $BRIDGE_PID 2>/dev/null || true
