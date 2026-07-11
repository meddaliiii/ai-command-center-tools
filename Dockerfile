# Dockerfile — لازم عشان نقدر نثبت برامج نظام (unrar, tesseract, node)
# الرانتايم العادي (Python) على Render ما يسمح بهذا، لذا نستخدم Docker.

# مرحلة مؤقتة: نستخرج Node.js من صورته الرسمية بدل تثبيته بسكربتات خارجية
FROM node:20-slim AS node_base

FROM python:3.11-slim

# نسخ Node.js وnpm من الصورة الرسمية (لتشغيل جسر Puter.js)
COPY --from=node_base /usr/local/bin/node /usr/local/bin/node
COPY --from=node_base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

# برامج النظام المطلوبة لدعم RAR وOCR (عربي + إنجليزي) والتحويل الصوتي المحلي
RUN apt-get update && apt-get install -y --no-install-recommends \
    unrar-free \
    tesseract-ocr \
    tesseract-ocr-ara \
    espeak-ng \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# تثبيت مكتبات بايثون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# تثبيت مكتبات جسر Node.js (Puter.js)
COPY bridge/package.json bridge/package.json
RUN cd bridge && npm install --omit=dev

# نسخ كود التطبيق
COPY server.py .
COPY bridge/server.js bridge/server.js
COPY start.sh .
RUN chmod +x start.sh

# Render يمرر PORT تلقائياً كمتغير بيئة، والسيرفر يقرأه بنفسه
CMD ["./start.sh"]
