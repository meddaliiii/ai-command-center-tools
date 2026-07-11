/**
 * Puter Bridge - سيرفر Node.js صغير يشتغل محلياً جنب سيرفر Python
 * ------------------------------------------------------------------
 * السبب: مكتبة Puter.js مبنية للعمل من JavaScript فقط (متصفح أو Node)،
 * ومو من بايثون مباشرة. هذا الجسر يستقبل طلبات HTTP محلية من سيرفر
 * Python (المستمع على منفذ آخر) وينفذها فعلياً عبر Puter.js، ثم يرجع
 * النتيجة الحقيقية (صورة أو رابط فيديو).
 *
 * يحتاج: PUTER_AUTH_TOKEN كمتغير بيئة (حساب Puter.com مجاني - راجع
 * PUTER_SETUP.md للحصول عليه).
 */

const express = require("express");
const { init } = require("@heyputer/puter.js/src/init.cjs");

const app = express();
app.use(express.json());

const PORT = process.env.BRIDGE_PORT || 3001;
const AUTH_TOKEN = process.env.PUTER_AUTH_TOKEN;

if (!AUTH_TOKEN) {
  console.error("[puter-bridge] تحذير: PUTER_AUTH_TOKEN غير مضبوط. الأدوات ستفشل حتى تضبطه.");
}

const puter = AUTH_TOKEN ? init(AUTH_TOKEN) : null;

app.get("/health", (req, res) => {
  res.json({ status: "ok", has_token: Boolean(AUTH_TOKEN) });
});

app.post("/generate-image", async (req, res) => {
  if (!puter) {
    return res.status(500).json({ success: false, error: "PUTER_AUTH_TOKEN غير مضبوط على السيرفر." });
  }
  const { prompt, model } = req.body || {};
  if (!prompt) {
    return res.status(400).json({ success: false, error: "مطلوب حقل prompt." });
  }
  try {
    const options = model ? { model } : {};
    const img = await puter.ai.txt2img(prompt, options);
    // img.src يحتوي إما data URI (data:image/...;base64,...) أو رابط مباشر
    return res.json({ success: true, src: img.src || img.toString() });
  } catch (err) {
    return res.status(500).json({
      success: false,
      error: (err && (err.message || err.error?.message)) || String(err),
    });
  }
});

app.post("/generate-video", async (req, res) => {
  if (!puter) {
    return res.status(500).json({ success: false, error: "PUTER_AUTH_TOKEN غير مضبوط على السيرفر." });
  }
  const { prompt } = req.body || {};
  if (!prompt) {
    return res.status(400).json({ success: false, error: "مطلوب حقل prompt." });
  }
  try {
    const video = await puter.ai.txt2vid(prompt);
    return res.json({ success: true, src: video.src || video.toString() });
  } catch (err) {
    return res.status(500).json({
      success: false,
      error: (err && (err.message || err.error?.message)) || String(err),
    });
  }
});

app.listen(PORT, "127.0.0.1", () => {
  console.log(`[puter-bridge] يستمع محلياً على المنفذ ${PORT}`);
});
