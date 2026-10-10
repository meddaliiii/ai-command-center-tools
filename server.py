"""
AI Command Center - External Tools MCP Server
------------------------------------------------
هدف هذا السيرفر: تنفيذ العمليات التي لا يقدر نموذج اللغة ينفذها بنفسه
(فتح روابط فيديو فعلياً، فك ضغط أرشيفات) ويرجّع بيانات حقيقية فقط.

مصمم للنشر المجاني على Render.com (Web Service) ثم ربطه كأداة MCP خارجية
داخل إعدادات تطبيق genspark (Tools / MCP Servers / Integrations).
"""

import base64
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image, Audio

mcp = FastMCP(
    "ai-command-center-tools",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

MAX_FILE_MB = 100
BRIDGE_URL = "http://127.0.0.1:3001"
PUBLIC_BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://ai-command-center-tools-v2.onrender.com")
DOWNLOADS_DIR = "/tmp/mcp_downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def _download_to_temp(url: str, tmp_dir: str, max_mb: int = MAX_FILE_MB) -> tuple[str | None, dict | None]:
    local_path = os.path.join(tmp_dir, "downloaded_file")
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        size = 0
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    return None, {"success": False, "error": f"الملف أكبر من الحد الأقصى المسموح ({max_mb}MB)."}
                f.write(chunk)
    except Exception as e:
        return None, {"success": False, "error": f"تعذّر تحميل الملف: {e}"}
    return local_path, None


def _cleanup_old_downloads(max_age_seconds: int = 3600):
    try:
        now = time.time()
        for fname in os.listdir(DOWNLOADS_DIR):
            fpath = os.path.join(DOWNLOADS_DIR, fname)
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > max_age_seconds:
                os.remove(fpath)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# أداة 1: تحليل رابط فيديو
# ──────────────────────────────────────────────────────────────────────

_whisper_model = None

def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _whisper_model


def _whisper_transcribe_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        try:
            import yt_dlp
        except ImportError:
            return {"success": False, "error": "مكتبة yt-dlp غير مثبتة على السيرفر."}

        audio_template = os.path.join(tmp_dir, "audio.%(ext)s")
        ydl_opts = {
            "quiet": True,
            "format": "worstaudio/worst",
            "outtmpl": audio_template,
            "max_filesize": 40 * 1024 * 1024,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "64"}],
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            return {"success": False, "error": f"تعذّر تحميل الصوت للتفريغ: {e}"}

        audio_files = list(Path(tmp_dir).glob("audio.*"))
        if not audio_files:
            return {"success": False, "error": "لم يتم العثور على ملف صوتي بعد التحميل."}

        try:
            model = _get_whisper_model()
            segments, info = model.transcribe(str(audio_files[0]), beam_size=1)
            text = " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            return {"success": False, "error": f"تعذّر تفريغ الصوت: {e}"}

        if not text:
            return {"success": False, "error": "لم يُكتشف كلام واضح بالفيديو."}

        return {
            "success": True,
            "transcript": text[:8000],
            "language_detected": getattr(info, "language", None),
            "method": "whisper-tiny (محلي)",
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def transcribe_video_url(url: str) -> dict:
    """
    يفرّغ الكلام المنطوق بفيديو إلى نص فعلياً عبر Whisper محلي.
    مخصص للفيديوهات التي لا تملك ترجمة جاهزة (تيك توك وغيرها).
    """
    return _whisper_transcribe_impl(url)


def _extract_transcript(info: dict) -> str | None:
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


def _analyze_video_url_impl(url: str) -> dict:
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
        "transcript_method": "captions" if transcript_text else None,
        "note": None if transcript_text else (
            "لا توجد ترجمة جاهزة. استدعِ transcribe_video_url إن لزم نص الكلام."
        ),
    }


@mcp.tool()
def analyze_video_url(url: str) -> dict:
    """
    يجلب بيانات حقيقية عن رابط فيديو: العنوان، الوصف، القناة، المدة،
    وترانسكريبت جاهز إن توفر. يدعم يوتيوب وتيك توك وإنستغرام وغيرها.
    """
    return _analyze_video_url_impl(url)


def _extract_video_frames_impl(url: str, num_frames: int = 4):
    if num_frames < 1 or num_frames > 8:
        num_frames = 4
    tmp_dir = tempfile.mkdtemp()
    try:
        try:
            from claude_real_video import process as crv_process
        except ImportError:
            return [{"success": False, "error": "مكتبة claude-real-video غير مثبتة على السيرفر."}]

        out_dir = os.path.join(tmp_dir, "crv-out")
        try:
            result = crv_process(url, out_dir, do_transcribe=False, max_frames=num_frames)
        except Exception as e:
            return [{"success": False, "error": f"تعذّر تحليل الفيديو بصرياً: {e}"}]

        frame_files = sorted(Path(result.frames_dir).glob("*.jpg")) if result.frames_dir and os.path.isdir(result.frames_dir) else []
        if not frame_files:
            return [{"success": False, "error": "لم يتم استخراج أي لقطات."}]

        images = []
        for f in frame_files[:num_frames]:
            with open(f, "rb") as fh:
                images.append(Image(data=fh.read(), format="jpeg"))

        summary = {
            "success": True,
            "duration_seconds": result.duration,
            "frame_count": len(images),
        }
        return [summary, *images]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def extract_video_frames(url: str, num_frames: int = 4):
    """
    يحمّل فيديو من رابط ويستخرج منه لقطات حقيقية (بحد أقصى 8 لقطات).
    """
    return _extract_video_frames_impl(url, num_frames)


# ──────────────────────────────────────────────────────────────────────
# أداة 2: فك ضغط أرشيف
# ──────────────────────────────────────────────────────────────────────

def _extract_archive_impl(file_url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        archive_path, err = _download_to_temp(file_url, tmp_dir, max_mb=MAX_FILE_MB)
        if err:
            return err

        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        infos = {}
        archive_type = None

        try:
            if zipfile.is_zipfile(archive_path):
                archive_type = "zip"
                with zipfile.ZipFile(archive_path) as zf:
                    infos = {i.filename: i.file_size for i in zf.infolist() if not i.is_dir()}
                    zf.extractall(extract_dir)
            else:
                import rarfile
                import py7zr
                if rarfile.is_rarfile(archive_path):
                    archive_type = "rar"
                    with rarfile.RarFile(archive_path) as rf:
                        infos = {i.filename: i.file_size for i in rf.infolist() if not i.is_dir()}
                        rf.extractall(extract_dir)
                elif py7zr.is_7zfile(archive_path):
                    archive_type = "7z"
                    with py7zr.SevenZipFile(archive_path, mode="r") as zf:
                        zf.extractall(extract_dir)
                    for root, _, files in os.walk(extract_dir):
                        for fn in files:
                            full = os.path.join(root, fn)
                            infos[os.path.relpath(full, extract_dir)] = os.path.getsize(full)
                else:
                    return {"success": False, "error": "صيغة الأرشيف غير مدعومة (ZIP, RAR, 7z فقط)."}
        except Exception as e:
            return {"success": False, "error": f"تعذّر فتح/فك الأرشيف: {e}"}

        results = []
        for name, size in infos.items():
            size_mb = size / (1024 * 1024)
            entry = {"name": name, "size_mb": round(size_mb, 2)}
            if size_mb > MAX_FILE_MB:
                entry["note"] = "تجاوز الحد الأقصى 100MB."
                results.append(entry)
                continue
            try:
                full_path = Path(extract_dir) / name
                if full_path.exists() and full_path.suffix.lower() in (".txt", ".md", ".json", ".csv", ".py", ".js", ".html"):
                    entry["excerpt"] = full_path.read_text(errors="ignore")[:500]
            except Exception as e:
                entry["note"] = f"فشل قراءة المحتوى: {e}"
            results.append(entry)

        return {"success": True, "archive_type": archive_type, "file_count": len(results), "files": results}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def extract_archive(file_url: str) -> dict:
    """
    يحمّل أرشيف (ZIP أو RAR أو 7z) من رابط ويفك ضغطه فعلياً.
    """
    return _extract_archive_impl(file_url)


def _create_archive_impl(files: list, archive_name: str = "archive") -> dict:
    if not files or not isinstance(files, list):
        return {"success": False, "error": "مطلوب قائمة files غير فارغة."}

    _cleanup_old_downloads()
    safe_name = re.sub(r"[^\w\-.]", "_", archive_name).strip("_") or "archive"
    file_id = f"{uuid.uuid4().hex[:12]}_{safe_name}.zip"
    zip_path = os.path.join(DOWNLOADS_DIR, file_id)
    total_size = 0
    added = []

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in files:
                name = item.get("name")
                if not name:
                    continue
                clean_name = os.path.normpath(name).lstrip("/\\")
                if clean_name.startswith(".."):
                    return {"success": False, "error": f"اسم ملف غير آمن: {name}"}
                if "content_base64" in item and item["content_base64"]:
                    data = base64.b64decode(item["content_base64"])
                elif "content" in item and item["content"] is not None:
                    data = str(item["content"]).encode("utf-8")
                else:
                    return {"success": False, "error": f"الملف '{name}' بدون content."}
                total_size += len(data)
                if total_size > MAX_FILE_MB * 1024 * 1024:
                    return {"success": False, "error": f"الحجم تجاوز {MAX_FILE_MB}MB."}
                zf.writestr(clean_name, data)
                added.append(clean_name)
    except Exception as e:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return {"success": False, "error": f"تعذّر إنشاء الأرشيف: {e}"}

    if not added:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return {"success": False, "error": "لم يتم إضافة أي ملف."}

    return {
        "success": True,
        "download_url": f"{PUBLIC_BASE_URL}/downloads/{file_id}",
        "file_count": len(added),
        "files": added,
        "size_kb": round(total_size / 1024, 1),
        "note": "الرابط صالح لمدة ساعة تقريباً.",
    }


@mcp.tool()
def create_archive(files: list, archive_name: str = "archive") -> dict:
    """
    ينشئ أرشيف ZIP من ملفات/نصوص ويرجع رابط تحميل مباشر.
    """
    return _create_archive_impl(files, archive_name)


# ──────────────────────────────────────────────────────────────────────
# أداة 3: تحليل صورة
# ──────────────────────────────────────────────────────────────────────

def _analyze_image_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=30)
        if err:
            return err
        try:
            from PIL import Image, ExifTags
            with Image.open(path) as img:
                exif_data = {}
                raw_exif = img.getexif()
                if raw_exif:
                    for tag_id, value in raw_exif.items():
                        tag = ExifTags.TAGS.get(tag_id, str(tag_id))
                        if isinstance(value, (str, int, float)):
                            exif_data[tag] = value
                return {
                    "success": True,
                    "format": img.format,
                    "width": img.width,
                    "height": img.height,
                    "mode": img.mode,
                    "file_size_kb": round(os.path.getsize(path) / 1024, 1),
                    "exif": exif_data or None,
                }
        except Exception as e:
            return {"success": False, "error": f"تعذّر قراءة الصورة: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def analyze_image_url(url: str) -> dict:
    """
    يجلب صورة من رابط ويرجع بياناتها: الأبعاد، الصيغة، حجم الملف، وبيانات EXIF.
    """
    return _analyze_image_impl(url)


def _ocr_image_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=30)
        if err:
            return err
        try:
            import pytesseract
            from PIL import Image
            text = pytesseract.image_to_string(Image.open(path), lang="ara+eng").strip()
            return {"success": True, "text": text[:8000], "has_text": bool(text)}
        except Exception as e:
            return {"success": False, "error": f"تعذّر تشغيل OCR: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def ocr_image_url(url: str) -> dict:
    """
    يستخرج أي نص مكتوب داخل صورة (عربي أو إنجليزي) عبر Tesseract OCR.
    """
    return _ocr_image_impl(url)


# ──────────────────────────────────────────────────────────────────────
# أداة 4: تحليل صوت
# ──────────────────────────────────────────────────────────────────────

def _analyze_audio_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=60)
        if err:
            return err
        try:
            import mutagen
            f = mutagen.File(path)
            if f is None:
                return {"success": False, "error": "الصيغة غير مدعومة أو الملف ليس صوتي."}
            tags = {}
            if f.tags:
                for key in ("title", "artist", "album", "date", "TIT2", "TPE1", "TALB"):
                    if key in f.tags:
                        tags[key] = str(f.tags[key])
            return {
                "success": True,
                "duration_seconds": round(f.info.length, 1) if hasattr(f.info, "length") else None,
                "bitrate_kbps": round(f.info.bitrate / 1000, 1) if hasattr(f.info, "bitrate") else None,
                "sample_rate": getattr(f.info, "sample_rate", None),
                "file_size_kb": round(os.path.getsize(path) / 1024, 1),
                "tags": tags or None,
            }
        except Exception as e:
            return {"success": False, "error": f"تعذّر قراءة الملف الصوتي: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def analyze_audio_url(url: str) -> dict:
    """
    يجلب ملف صوتي من رابط ويرجع بياناته: المدة، معدل البت، والبيانات الوصفية.
    """
    return _analyze_audio_impl(url)


# ──────────────────────────────────────────────────────────────────────
# أداة 5: قراءة مستندات
# ──────────────────────────────────────────────────────────────────────

def _read_document_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=50)
        if err:
            return err
        ext = Path(url.split("?")[0]).suffix.lower()
        try:
            if ext == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(path)
                text = "\n".join((p.extract_text() or "") for p in reader.pages[:30])
                return {"success": True, "type": "pdf", "page_count": len(reader.pages), "text": text[:12000], "truncated": len(text) > 12000}
            elif ext == ".docx":
                import docx
                d = docx.Document(path)
                text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
                return {"success": True, "type": "docx", "paragraph_count": len(d.paragraphs), "text": text[:12000], "truncated": len(text) > 12000}
            elif ext in (".txt", ".md", ".csv", ".json", ".py", ".js", ".html", ".xml"):
                text = Path(path).read_text(errors="ignore")
                return {"success": True, "type": ext.lstrip("."), "text": text[:12000], "truncated": len(text) > 12000}
            elif ext == ".xlsx":
                import io
                import openpyxl
                with open(path, "rb") as fh:
                    buf = io.BytesIO(fh.read())
                wb = openpyxl.load_workbook(buf, data_only=True, read_only=True)
                sheets_preview = {}
                for sheet_name in wb.sheetnames[:5]:
                    ws = wb[sheet_name]
                    rows = []
                    for i, row in enumerate(ws.iter_rows(values_only=True)):
                        if i >= 50:
                            break
                        rows.append([("" if c is None else str(c)) for c in row])
                    sheets_preview[sheet_name] = rows
                return {"success": True, "type": "xlsx", "sheet_names": wb.sheetnames, "sheets_preview": sheets_preview}
            elif ext == ".pptx":
                import pptx
                prs = pptx.Presentation(path)
                slides = []
                for i, slide in enumerate(prs.slides):
                    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame and shape.text_frame.text.strip()]
                    slides.append({"slide": i + 1, "text": "\n".join(texts)})
                return {"success": True, "type": "pptx", "slide_count": len(slides), "slides": slides}
            else:
                return {"success": False, "error": f"صيغة '{ext}' غير مدعومة (pdf, docx, xlsx, pptx, txt, md, csv, json)."}
        except Exception as e:
            return {"success": False, "error": f"تعذّر قراءة المستند: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def read_document_url(url: str) -> dict:
    """
    يحمّل مستند من رابط (PDF, DOCX, TXT, MD, CSV, JSON, XLSX, PPTX) ويستخرج نصه.
    """
    return _read_document_impl(url)


# ──────────────────────────────────────────────────────────────────────
# أداة 6: تحليل APK
# ──────────────────────────────────────────────────────────────────────

def _analyze_apk_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=100)
        if err:
            return err
        try:
            from androguard.core.apk import APK
            apk = APK(path)
            return {
                "success": True,
                "package_name": apk.get_package(),
                "app_name": apk.get_app_name(),
                "version_name": apk.get_androidversion_name(),
                "version_code": apk.get_androidversion_code(),
                "min_sdk": apk.get_min_sdk_version(),
                "target_sdk": apk.get_target_sdk_version(),
                "permissions": apk.get_permissions()[:50],
                "permission_count": len(apk.get_permissions()),
                "is_signed": apk.is_signed(),
            }
        except Exception as e:
            return {"success": False, "error": f"تعذّر تحليل APK: {e}"}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def analyze_apk_url(url: str) -> dict:
    """
    يحمّل ملف APK ويحلله: اسم الحزمة، الإصدار، قائمة الصلاحيات.
    """
    return _analyze_apk_impl(url)


# ──────────────────────────────────────────────────────────────────────
# أداة 7: جلب صفحة ويب
# ──────────────────────────────────────────────────────────────────────

def _fetch_webpage_impl(url: str) -> dict:
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (compatible; AICommandCenterBot/1.0)"})
        r.raise_for_status()
    except Exception as e:
        return {"success": False, "error": f"تعذّر جلب الصفحة: {e}"}

    title = None
    text = None
    try:
        import trafilatura
        text = trafilatura.extract(r.content, include_comments=False, include_tables=True)
        meta = trafilatura.extract_metadata(r.content)
        if meta:
            title = meta.title
    except Exception:
        pass

    if not text:
        r.encoding = r.encoding or r.apparent_encoding
        stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.DOTALL)
        stripped = re.sub(r"<[^<]+?>", " ", stripped)
        text = re.sub(r"\s+", " ", stripped).strip()

    if not text:
        return {"success": False, "error": "تعذّر استخراج نص مفيد (قد تعتمد على JavaScript)."}

    return {"success": True, "title": title, "text": text[:12000], "truncated": len(text) > 12000}


@mcp.tool()
def fetch_webpage_url(url: str) -> dict:
    """
    يجلب صفحة ويب ويستخرج نصها الحقيقي بدون إعلانات أو قوائم تنقل.
    """
    return _fetch_webpage_impl(url)


# ──────────────────────────────────────────────────────────────────────
# أداة 8: تحويل نص إلى صوت
# ──────────────────────────────────────────────────────────────────────

def _generate_speech_bytes(text: str, lang: str = "ar"):
    if not text or not text.strip():
        return None, {"success": False, "error": "النص فارغ."}
    if len(text) > 2000:
        return None, {"success": False, "error": "النص أطول من 2000 حرف."}

    tmp_dir = tempfile.mkdtemp()
    try:
        out_path = os.path.join(tmp_dir, "speech.wav")
        voice = "ar" if lang.lower().startswith("ar") else "en"
        try:
            import subprocess
            subprocess.run(["espeak-ng", "-v", voice, "-s", "150", text, "-w", out_path],
                           check=True, capture_output=True, timeout=30)
        except FileNotFoundError:
            return None, {"success": False, "error": "محرك espeak-ng غير مثبت."}
        except Exception as e:
            return None, {"success": False, "error": f"تعذّر توليد الصوت: {e}"}

        if not os.path.exists(out_path):
            return None, {"success": False, "error": "لم يتم إنشاء ملف الصوت."}

        with open(out_path, "rb") as f:
            return f.read(), None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def text_to_speech(text: str, lang: str = "ar"):
    """
    يحوّل نصاً إلى ملف صوتي WAV عبر espeak-ng (محلي، بدون API خارجي).
    lang: "ar" للعربية أو "en" للإنجليزية. حد أقصى 2000 حرف.
    """
    audio_bytes, err = _generate_speech_bytes(text, lang)
    if err:
        return err
    return Audio(data=audio_bytes, format="wav")


# ──────────────────────────────────────────────────────────────────────
# أداة 9 و10: توليد صورة/فيديو عبر Puter.js
# ──────────────────────────────────────────────────────────────────────

def _call_bridge(endpoint: str, payload: dict, timeout: int = 90):
    try:
        r = requests.post(f"{BRIDGE_URL}{endpoint}", json=payload, timeout=timeout)
        return r.json(), None
    except requests.exceptions.ConnectionError:
        return None, {"success": False, "error": "جسر Puter (Node.js) غير متاح على السيرفر."}
    except Exception as e:
        return None, {"success": False, "error": f"تعذّر الاتصال بجسر التوليد: {e}"}


def _data_uri_to_bytes(data_uri: str):
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return None
    try:
        _, b64data = data_uri.split(",", 1)
        return base64.b64decode(b64data)
    except Exception:
        return None


def _generate_image_impl(prompt: str, model: str = None):
    payload = {"prompt": prompt}
    if model:
        payload["model"] = model
    result, err = _call_bridge("/generate-image", payload)
    if err:
        return [err]
    if not result.get("success"):
        return [result]
    src = result.get("src", "") or ""
    img_bytes = _data_uri_to_bytes(src)
    if img_bytes:
        return [{"success": True, "prompt": prompt}, Image(data=img_bytes, format="png")]
    if src.startswith("http"):
        try:
            r = requests.get(src, timeout=30)
            r.raise_for_status()
            return [{"success": True, "prompt": prompt}, Image(data=r.content, format="png")]
        except Exception as e:
            return [{"success": True, "prompt": prompt, "image_url": src}]
    return [{"success": False, "error": "لم يُرجع الجسر بيانات صورة صالحة."}]


@mcp.tool()
def generate_image(prompt: str, model: str = None):
    """
    يولّد صورة حقيقية بالذكاء الاصطناعي من وصف نصي عبر Puter.js.
    model: اختياري مثل "gpt-image-2" أو "black-forest-labs/flux-2-pro".
    """
    return _generate_image_impl(prompt, model)


def _generate_video_impl(prompt: str) -> dict:
    result, err = _call_bridge("/generate-video", {"prompt": prompt}, timeout=180)
    if err:
        return err
    if not result.get("success"):
        return result
    return {
        "success": True,
        "prompt": prompt,
        "video_url": result.get("src"),
        "note": "افتح الرابط لمشاهدة الفيديو. التوليد قد يأخذ دقيقة أو أكثر.",
    }


@mcp.tool()
def generate_video(prompt: str) -> dict:
    """
    يولّد فيديو قصير حقيقي بالذكاء الاصطناعي من وصف نصي عبر Puter.js.
    """
    return _generate_video_impl(prompt)


# ══════════════════════════════════════════════════════════════════════
# ████  أدوات .so — تحليل وتعديل وإعادة تجميع  ████
# ══════════════════════════════════════════════════════════════════════

def _run_cmd(cmd: list, timeout: int = 30) -> str:
    """تشغيل أمر نظام وإرجاع المخرجات."""
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"خطأ: {e}"


def _analyze_so_impl(url: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=50)
        if err:
            return err

        # معلومات أساسية بأدوات النظام
        file_info   = _run_cmd(["file", path])
        strings_raw = _run_cmd(["strings", "-n", "6", path])

        # تحليل منظم بـ pyelftools
        sections = []
        imports  = []
        exports  = []
        arch     = None
        entry    = None

        try:
            from elftools.elf.elffile import ELFFile
            from elftools.elf.sections import SymbolTableSection

            with open(path, "rb") as f:
                elf = ELFFile(f)
                arch  = elf.get_machine_arch()
                entry = hex(elf.header.e_entry)

                for s in elf.iter_sections():
                    sections.append({
                        "name":   s.name,
                        "type":   s["sh_type"],
                        "size":   s["sh_size"],
                        "offset": hex(s["sh_offset"]),
                        "addr":   hex(s["sh_addr"]),
                    })

                for s in elf.iter_sections():
                    if isinstance(s, SymbolTableSection):
                        for sym in s.iter_symbols():
                            if not sym.name:
                                continue
                            bind = sym.entry.st_info.bind
                            if sym.entry.st_shndx == "SHN_UNDEF":
                                imports.append(sym.name)
                            elif bind in ("STB_GLOBAL", "STB_WEAK"):
                                exports.append(sym.name)

        except ImportError:
            pass  # pyelftools غير متاح، نرجع النتائج الخام

        strings_list = [s for s in strings_raw.splitlines() if len(s) > 5]

        return {
            "success": True,
            "arch": arch,
            "entry_point": entry,
            "file_info": file_info,
            "section_count": len(sections),
            "sections": sections[:50],
            "export_count": len(exports),
            "exports": exports[:100],
            "import_count": len(imports),
            "imports": imports[:100],
            "readable_strings": strings_list[:150],
            "tip": "استخدم patch_so_bytes لتعديل offset محدد، أو patch_so_string لاستبدال نص.",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def analyze_so_url(url: str) -> dict:
    """
    يحلل ملف .so (ELF shared library) من رابط تحميل مباشر.
    يرجع: المعمارية، entry point، الـ sections، الدوال المصدّرة (exports)،
    الدوال المستوردة (imports)، والنصوص القابلة للقراءة (strings).
    ابدأ دائماً بهذه الأداة قبل أي تعديل لفهم بنية الملف.
    """
    return _analyze_so_impl(url)


def _patch_so_bytes_impl(url: str, offset_hex: str, new_bytes_hex: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=50)
        if err:
            return err

        try:
            offset    = int(offset_hex, 16)
            new_bytes = bytes.fromhex(new_bytes_hex.replace(" ", ""))
        except ValueError as e:
            return {"success": False, "error": f"قيمة offset أو bytes غير صحيحة: {e}"}

        with open(path, "rb") as f:
            original = bytearray(f.read())

        file_size = len(original)
        if offset + len(new_bytes) > file_size:
            return {"success": False, "error": f"الـ offset ({hex(offset)}) يتجاوز حجم الملف ({file_size} bytes)."}

        old_bytes = bytes(original[offset: offset + len(new_bytes)])
        original[offset: offset + len(new_bytes)] = new_bytes

        patched_path = os.path.join(tmp_dir, "patched.so")
        with open(patched_path, "wb") as f:
            f.write(original)

        # رفع الملف المعدّل
        _cleanup_old_downloads()
        file_id = f"{uuid.uuid4().hex[:12]}_patched.so"
        dest = os.path.join(DOWNLOADS_DIR, file_id)
        shutil.copy(patched_path, dest)

        return {
            "success": True,
            "offset": offset_hex,
            "original_bytes": old_bytes.hex(),
            "new_bytes": new_bytes_hex.replace(" ", ""),
            "bytes_changed": len(new_bytes),
            "download_url": f"{PUBLIC_BASE_URL}/downloads/{file_id}",
            "note": "الرابط صالح لمدة ساعة. تحقق من الملف قبل إعادة التجميع.",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def patch_so_bytes(url: str, offset_hex: str, new_bytes_hex: str) -> dict:
    """
    يعدّل bytes محددة في ملف .so بالموضع (offset) ويرجع الملف المعدّل.
    url: رابط ملف .so الأصلي
    offset_hex: الموضع كـ hex مثل "0x1a2b"
    new_bytes_hex: الـ bytes الجديدة كـ hex مثل "9090" (NOP NOP في x86)
    استخدم analyze_so_url أولاً لمعرفة الـ offsets الصحيحة.
    """
    return _patch_so_bytes_impl(url, offset_hex, new_bytes_hex)


def _patch_so_string_impl(url: str, old_string: str, new_string: str) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(url, tmp_dir, max_mb=50)
        if err:
            return err

        with open(path, "rb") as f:
            data = bytearray(f.read())

        old_b = old_string.encode("utf-8")
        new_b = new_string.encode("utf-8")

        if len(new_b) > len(old_b):
            return {
                "success": False,
                "error": (
                    f"النص الجديد ({len(new_b)} bytes) أطول من الأصلي ({len(old_b)} bytes). "
                    "يجب أن يكون بنفس الطول أو أقصر (يُكمَّل بـ null bytes)."
                ),
            }

        idx = data.find(old_b)
        if idx == -1:
            return {"success": False, "error": f"لم يُعثر على النص '{old_string}' في الملف."}

        # استبدال مع padding بـ null bytes
        padded = new_b + b"\x00" * (len(old_b) - len(new_b))
        data[idx: idx + len(old_b)] = padded

        # حفظ ورفع
        patched_path = os.path.join(tmp_dir, "patched.so")
        with open(patched_path, "wb") as f:
            f.write(data)

        _cleanup_old_downloads()
        file_id = f"{uuid.uuid4().hex[:12]}_strpatch.so"
        dest = os.path.join(DOWNLOADS_DIR, file_id)
        shutil.copy(patched_path, dest)

        # التحقق من وجود نسخ أخرى من نفس النص
        remaining = data.count(old_b)

        return {
            "success": True,
            "found_at_offset": hex(idx),
            "old_string": old_string,
            "new_string": new_string,
            "remaining_occurrences": remaining,
            "download_url": f"{PUBLIC_BASE_URL}/downloads/{file_id}",
            "note": (
                "الرابط صالح لمدة ساعة. "
                + (f"تنبيه: يوجد {remaining} نسخة أخرى من نفس النص في الملف." if remaining > 0 else "لا توجد نسخ أخرى من النص.")
            ),
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def patch_so_string(url: str, old_string: str, new_string: str) -> dict:
    """
    يبحث عن نص (string) داخل ملف .so ويستبدله بنص جديد،
    ويرجع الملف المعدّل كرابط تحميل.
    النص الجديد يجب أن يكون بنفس طول الأصلي أو أقصر.
    مفيد لتغيير URLs أو رسائل أو معرّفات داخل المكتبة.
    """
    return _patch_so_string_impl(url, old_string, new_string)


def _extract_so_from_apk_impl(apk_url: str, arch: str = None) -> dict:
    tmp_dir = tempfile.mkdtemp()
    try:
        path, err = _download_to_temp(apk_url, tmp_dir, max_mb=100)
        if err:
            return err

        if not zipfile.is_zipfile(path):
            return {"success": False, "error": "الملف ليس APK صالح (يجب أن يكون ZIP)."}

        found = []
        with zipfile.ZipFile(path) as zf:
            for item in zf.infolist():
                if not item.filename.endswith(".so"):
                    continue
                if arch and arch not in item.filename:
                    continue

                extract_dir = os.path.join(tmp_dir, "apk_out")
                os.makedirs(extract_dir, exist_ok=True)
                zf.extract(item, extract_dir)
                so_path = os.path.join(extract_dir, item.filename)

                _cleanup_old_downloads()
                file_id = f"{uuid.uuid4().hex[:8]}_{os.path.basename(item.filename)}"
                dest = os.path.join(DOWNLOADS_DIR, file_id)
                shutil.copy(so_path, dest)

                found.append({
                    "name": os.path.basename(item.filename),
                    "path_in_apk": item.filename,
                    "size_kb": round(item.file_size / 1024, 1),
                    "download_url": f"{PUBLIC_BASE_URL}/downloads/{file_id}",
                })

        if not found:
            msg = "لم يُعثر على ملفات .so"
            if arch:
                msg += f" للمعمارية '{arch}'"
            msg += ". المعماريات الشائعة: arm64-v8a, armeabi-v7a, x86, x86_64"
            return {"success": False, "error": msg}

        return {
            "success": True,
            "so_count": len(found),
            "files": found,
            "next_step": "استخدم analyze_so_url لتحليل أي ملف، أو patch_so_bytes/patch_so_string للتعديل.",
            "note": "الروابط صالحة لمدة ساعة.",
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@mcp.tool()
def extract_so_from_apk(apk_url: str, arch: str = None) -> dict:
    """
    يستخرج ملفات .so من داخل APK ويرجع رابط تحميل لكل ملف.
    apk_url: رابط ملف APK
    arch: اختياري لتصفية معمارية معينة مثل "arm64-v8a" أو "armeabi-v7a"
    بعد الاستخراج استخدم analyze_so_url لتحليل الملف،
    ثم patch_so_bytes أو patch_so_string للتعديل.
    """
    return _extract_so_from_apk_impl(apk_url, arch)


# ──────────────────────────────────────────────────────────────────────
# نقطة تحميل الملفات المُنشأة
# ──────────────────────────────────────────────────────────────────────

@mcp.custom_route("/downloads/{file_id}", methods=["GET"])
async def download_file(request):
    from starlette.responses import FileResponse, JSONResponse
    file_id = request.path_params.get("file_id", "")
    safe_id = os.path.basename(file_id)
    file_path = os.path.join(DOWNLOADS_DIR, safe_id)
    if not os.path.isfile(file_path):
        return JSONResponse({"success": False, "error": "الملف غير موجود أو انتهت صلاحيته."}, status_code=404)
    # تحديد نوع المحتوى
    if safe_id.endswith(".so"):
        media_type = "application/octet-stream"
    else:
        media_type = "application/zip"
    return FileResponse(file_path, media_type=media_type, filename=safe_id)


# ──────────────────────────────────────────────────────────────────────
# نقاط REST عادية (GET)
# ──────────────────────────────────────────────────────────────────────

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

@mcp.custom_route("/api/analyze-image", methods=["GET"])
async def api_analyze_image(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_analyze_image_impl(url))

@mcp.custom_route("/api/ocr-image", methods=["GET"])
async def api_ocr_image(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_ocr_image_impl(url))

@mcp.custom_route("/api/analyze-audio", methods=["GET"])
async def api_analyze_audio(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_analyze_audio_impl(url))

@mcp.custom_route("/api/read-document", methods=["GET"])
async def api_read_document(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_read_document_impl(url))

@mcp.custom_route("/api/analyze-apk", methods=["GET"])
async def api_analyze_apk(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_analyze_apk_impl(url))

@mcp.custom_route("/api/fetch-webpage", methods=["GET"])
async def api_fetch_webpage(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_fetch_webpage_impl(url))

@mcp.custom_route("/api/text-to-speech", methods=["GET"])
async def api_text_to_speech(request):
    from starlette.responses import JSONResponse
    text = request.query_params.get("text")
    lang = request.query_params.get("lang", "ar")
    if not text:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر text"}, status_code=400)
    audio_bytes, err = _generate_speech_bytes(text, lang)
    if err:
        return JSONResponse(err)
    return JSONResponse({
        "success": True,
        "format": "wav",
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
    })

@mcp.custom_route("/api/analyze-so", methods=["GET"])
async def api_analyze_so(request):
    from starlette.responses import JSONResponse
    url = request.query_params.get("url")
    if not url:
        return JSONResponse({"success": False, "error": "مطلوب باراميتر url"}, status_code=400)
    return JSONResponse(_analyze_so_impl(url))


# ──────────────────────────────────────────────────────────────────────
# نقطة فحص صحة السيرفر
# ──────────────────────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "status": "ok",
        "tools": [
            "analyze_video_url",
            "transcribe_video_url",
            "extract_video_frames",
            "extract_archive",
            "create_archive",
            "analyze_image_url",
            "ocr_image_url",
            "analyze_audio_url",
            "read_document_url",
            "analyze_apk_url",
            "fetch_webpage_url",
            "text_to_speech",
            "generate_image",
            "generate_video",
            # أدوات .so الجديدة
            "analyze_so_url",
            "patch_so_bytes",
            "patch_so_string",
            "extract_so_from_apk",
        ],
        "version": "2.1.0",
    })
