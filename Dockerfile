# Dockerfile — لازم عشان نقدر نثبت برامج نظام (unrar, tesseract)
# الرانتايم العادي (Python) على Render ما يسمح بهذا، لذا نستخدم Docker.

FROM python:3.11-slim

# برامج النظام المطلوبة لدعم RAR وOCR (عربي + إنجليزي)
RUN apt-get update && apt-get install -y --no-install-recommends \
    unrar-free \
    tesseract-ocr \
    tesseract-ocr-ara \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Render يمرر PORT تلقائياً كمتغير بيئة، والسيرفر يقرأه بنفسه
CMD ["python", "server.py"]
