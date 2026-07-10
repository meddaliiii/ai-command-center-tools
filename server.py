"""
AI Command Center - External Tools MCP Server
------------------------------------------------
هدف هذا السيرفر: تنفيذ العمليات التي لا يقدر نموذج اللغة ينفذها بنفسه
(فتح روابط فيديو فعلياً، فك ضغط أرشيفات) ويرجّع بيانات حقيقية فقط.
مصمم للنشر المجاني على Render.com (Web Service) ثم ربطه كأداة خارجية
داخل إعدادات تطبيق genspark (Tools / MCP Servers / Integrations / قراءة الروابط).

التشغيل محلياً للاختبار:
    pip install -r requirements.txt
    python server.py
ثم اختبر بمتصفح: GET /health

النشر على Render:
    Build Command:  pip install -r requirements.txt
    Start Command:  python server.py
    (Render يمرر متغير البيئة PORT تلقائياً، السيرفر يقرأه بنفسه)
"""

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "ai-command-center-tools",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

MAX_FILE_MB = 100
YOUTUBE_TIKTOK_INSTAGRAM = re.compile(
    r"(youtu\.be/|youtube\.com/(watch|shorts)|tiktok\.com/|instagram\.com/reel)"
)


# ----------------------------------------------------------------------
# أداة 1: تحليل رابط فيديو (يوتيوب / تيك توك / إنستغرام)
# ----------------------------------------------------------------------
def _analyze_video_url_impl(url: str) -> dict:
    if not YOUTUBE_TIKTOK_INSTAGRAM.search(url):
        return {"success": False, "error": "الرابط غير مدعوم حالياً (يوتيوب/تيك توك/إنستغرام فقط)."}

    try:
        import yt_dlp
    except ImportError:
        return {"success": False, "error": "مكتبة yt-dlp غير مثبتة على السيرفر."}

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ar", "en"],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return {"success": False, "error": f"تعذّر جلب الفيديو: {e}"}

    transcript_text = _extract_transcript(info)

    return {
        "success": True,
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration_seconds": info.get("duration"),
        "description": (info.get("description") or "")[:2000],
        "transcript_available": bool(transcript_text),
        "transcript": transcript_text[:8000] if transcript_text else None,
    }


@mcp.tool()
def analyze_video_url(url: str) -> dict:
    """
    يجلب بيانات حقيقية عن رابط فيديو: العنوان، الوصف، القناة، المدة،
    والترانسكريبت إن توفر (captions أو auto-captions).
    لا يخمّن أبداً: إذا فشل الجلب يرجع success=False مع سبب واضح.
    """
    return _analyze_video_url_impl(url)


def _extract_transcript(info: dict) -> str | None:
    """يحاول استخراج نص الترجمة من subtitles أو automatic_captions إن وُجدت."""
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key) or {}
        for lang in ("ar", "en"):
            if lang in tracks:
                for fmt in tracks[lang]:
                    if fmt.get("ext") in ("vtt", "srv1", "srt") and fmt.get("url"):
                        try:
                            r = requests.get(fmt["url"], timeout=15)
                            if r.ok:
                                return _clean_subtitle_text(r.text)
                        except Exception:
                            continue
    return None


def _clean_subtitle_text(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if line.isdigit():
            continue
        lines.append(re.sub(r"<[^>]+>", "", line))
    seen, out = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return " ".join(out)


# ----------------------------------------------------------------------
# أداة 2: فك ضغط أرشيف (ZIP) من رابط وإرجاع محتواه الفعلي
# ----------------------------------------------------------------------
def _extract_archive_impl(file_url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, "archive.zip")
        try:
            r = requests.get(file_url, timeout=60, stream=True)
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        except Exception as e:
            return {"success": False, "error": f"تعذّر تحميل الملف: {e}"}

        if not zipfile.is_zipfile(zip_path):
            return {"success": False, "error": "الملف ليس أرشيف ZIP صالح (RAR/7z تحتاج مكتبة إضافية)."}

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        results = []
        with zipfile.ZipFile(zip_path) as zf:
            for item in zf.infolist():
                if item.is_dir():
                    continue
                size_mb = item.file_size / (1024 * 1024)
                entry = {"name": item.filename, "size_mb": round(size_mb, 2)}
                if size_mb > MAX_FILE_MB:
                    entry["note"] = "تم تجاوز الحد الأقصى 100MB، لم تتم معالجته."
                    results.append(entry)
                    continue
                try:
                    zf.extract(item, extract_dir)
                    full_path = Path(extract_dir) / item.filename
                    if full_path.suffix.lower() in (".txt", ".md", ".json", ".csv", ".py", ".js", ".html"):
                        entry["excerpt"] = full_path.read_text(errors="ignore")[:500]
                except Exception as e:
                    entry["note"] = f"فشل الاستخراج: {e}"
                results.append(entry)

        return {"success": True, "file_count": len(results), "files": results}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def extract_archive(file_url: str) -> dict:
    """
    يحمّل أرشيف ZIP من رابط ويفك ضغطه فعلياً، ويرجع قائمة الملفات
    الداخلية مع مقتطف نصي من كل ملف نصي (حد أقصى 100MB لكل ملف).
    """
    return _extract_archive_impl(file_url)


# ----------------------------------------------------------------------
# نقاط REST عادية (GET) — لتُستخدم إذا كان لدى genspark ميزة عامة
# "قراءة رابط" تفتح أي URL وتقرأ محتواه، بدل بروتوكول MCP الكامل.
# مثال: GET /api/analyze-video?url=https://youtu.be/xxxx
# ----------------------------------------------------------------------
@mcp.custom_route("/api/analyze-video", methods=["GET"])
async def api_analyze_video(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_analyze_video_url_impl(url))


@mcp.custom_route("/api/extract-archive", methods=["GET"])
async def api_extract_archive(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_extract_archive_impl(url))


# ----------------------------------------------------------------------
# نقطة فحص صحة السيرفر (يُستخدم للتأكد أنه يعمل قبل ربطه بـ genspark)
# ----------------------------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "ok",
        "mcp_tools": ["analyze_video_url", "extract_archive"],
        "rest_endpoints": ["/api/analyze-video?url=...", "/api/extract-archive?url=..."],
    })


if __name__ == "__main__":
    # streamable-http = بروتوكول MCP عبر HTTP، هذا ما تحتاجه genspark كسيرفر MCP خارجي (remote)
    # ملاحظة: host/port يُقرآن من إعداد FastMCP نفسه أعلاه (وليس من run())
    mcp.run(transport="streamable-http") 
