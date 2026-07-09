# خطوات النشر (5 دقائق، مجاني بالكامل)

## 1. الرفع على GitHub
ارفع هذا المجلد (server.py + requirements.txt) إلى مستودع جديد في حسابك meddaliiii.

## 2. النشر على Render.com
- New → Web Service → اختر المستودع
- Build Command: `pip install -r requirements.txt`
- Start Command: `python server.py`
- Instance Type: Free
- بعد النشر ستحصل على رابط ثابت مثل:
  `https://ai-command-center-tools.onrender.com`

## 3. التأكد أنه يعمل فعلاً (قبل ربطه بـ genspark)
افتح في المتصفح:
`https://ai-command-center-tools.onrender.com/health`
يجب أن يظهر: `{"status": "ok", "tools": [...]}`
إذا ما ظهر هذا → المشكلة بالسيرفر نفسه، لا تكمل للخطوة التالية.

## 4. الربط داخل genspark
داخل إعدادات تطبيق "AI Command Center":
ابحث عن قسم باسم أحد هذه: **Tools** / **MCP Servers** / **Integrations** / **Developer Mode** / **Custom Actions**
- إن وجدت خيار "MCP Server (remote/HTTP)": أضف الرابط
  `https://ai-command-center-tools.onrender.com/mcp`
- إن لم تجد خيار MCP صريح ووجدت بدلاً منه "Custom Action / Webhook / Function":
  أضف الأداتين كنقطتي REST يدوياً بنفس المسارات، أو أخبرني وأبني لك نسخة FastAPI عادية بدل MCP (أبسط للربط اليدوي).

## 5. تبسيط البرومبت
بعد الربط، برومبت التطبيق ما يحتاج يشرح "كيف" (yt-dlp، إلخ) — فقط:
"إذا استلمت رابط فيديو → استدعِ analyze_video_url وارجع نتيجته الحقيقية فقط.
إذا استلمت ملف ZIP → استدعِ extract_archive.
إن رجعت success:false → اعرض رسالة الخطأ حرفياً، ممنوع التخمين."

## ملاحظة مهمة
ملف الفيديو المرفوع مباشرة (Image 1 عندك) لازم أول شيء يكون رابط عام (public URL)
حتى يقدر السيرفر يحمّله — تأكد أن genspark يرفع الملف لمكان يعطيه رابط مباشر
قبل ما يرسله لهذه الأداة، وإلا الأداة نفسها ما راح توصل للملف.
