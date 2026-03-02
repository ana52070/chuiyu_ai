import crypto from 'crypto';
import { waitUntil } from '@vercel/functions';
import fs from 'fs';
import path from 'path';

const CORP_ID     = process.env.WXWORK_CORP_ID;
const AGENT_ID    = process.env.WXWORK_AGENT_ID;
const CORP_SECRET = process.env.WXWORK_SECRET;
const WX_TOKEN    = process.env.WXWORK_TOKEN;
const WX_AES_KEY  = Buffer.from((process.env.WXWORK_AES_KEY || '') + '=', 'base64');

const SILICONFLOW_KEY = process.env.SILICONFLOW_KEY;

// VPS nginx 代理地址，所有发往企业微信的请求都走这里
const VPS_PROXY = 'http://49.233.85.74:8080';

/** ===================== 工具函数：企业微信解密/校验 ===================== */
function verifySignature(signature, timestamp, nonce, data = '') {
  const str = [WX_TOKEN, timestamp, nonce, data].sort().join('');
  return crypto.createHash('sha1').update(str).digest('hex') === signature;
}

function wxDecrypt(encrypted) {
  const buf = Buffer.from(encrypted, 'base64');
  const decipher = crypto.createDecipheriv('aes-256-cbc', WX_AES_KEY, WX_AES_KEY.slice(0, 16));
  decipher.setAutoPadding(false);
  const dec = Buffer.concat([decipher.update(buf), decipher.final()]);
  const pad = dec[dec.length - 1];
  const content = dec.slice(16, dec.length - pad);
  const msgLen = content.readUInt32BE(0);
  return content.slice(4, 4 + msgLen).toString('utf-8');
}

function getXmlValue(xml, tag) {
  const m = xml.match(new RegExp(`<${tag}><!\\[CDATA\\[([\\s\\S]*?)\\]\\]><\\/${tag}>|<${tag}>([\\s\\S]*?)<\\/${tag}>`));
  return m ? (m[1] ?? m[2] ?? '') : '';
}

function getRawBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', chunk => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
    req.on('error', reject);
  });
}

/** ===================== 企业微信：token + 发送 ===================== */
async function getAccessToken() {
  const res = await fetch(`${VPS_PROXY}/cgi-bin/gettoken?corpid=${CORP_ID}&corpsecret=${CORP_SECRET}`);
  const data = await res.json();
  console.log('[TOKEN] errcode:', data.errcode, 'token_prefix:', data.access_token?.slice(0, 10));
  return data.access_token;
}

async function sendMessageWithToken(token, toUser, content) {
  console.log('[SEND] 发送消息，长度:', content.length);
  const res = await fetch(`${VPS_PROXY}/cgi-bin/message/send?access_token=${token}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      touser: toUser,
      msgtype: 'text',
      agentid: parseInt(AGENT_ID),
      text: { content }
    })
  });
  const result = await res.json();
  console.log('[SEND] 企业微信返回:', JSON.stringify(result));
  return result;
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}
function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

/** ===================== 输出过滤/解析/挡板 ===================== */
const BAD_TOKENS = new Set([
  '[动画表情]', '[表情]', '[图片]', '[语音]', '[视频]', '[文件]',
  '[位置]', '[名片]', '[链接]', '[红包]', '[转账]'
]);

function cleanBubble(s) {
  if (!s) return '';
  let t = String(s).trim();
  if (!t) return '';
  if (BAD_TOKENS.has(t)) return '';

  // 去掉类似 [捂脸] [偷笑] 等
  t = t.replace(/\[[^\]]{1,12}\]/g, '').trim();
  if (!t) return '';

  // 单条不要太长
  if (t.length > 60) t = t.slice(0, 60);

  return t;
}

function parseMessagesFromModel(text) {
  // 期望严格 JSON：{"messages":[...]}
  try {
    const obj = JSON.parse(text);
    const arr = obj?.messages;
    if (Array.isArray(arr)) {
      const cleaned = arr.map(cleanBubble).filter(Boolean);
      return cleaned.slice(0, 4);
    }
  } catch (e) {}

  // fallback：按行切
  const lines = String(text).split('\n').map(x => cleanBubble(x)).filter(Boolean);
  return lines.slice(0, 3);
}

function qualityGuard(messages) {
  if (!messages || messages.length === 0) return false;
  if (messages.length > 4) return false;

  // 去重（防复读）
  const uniq = [];
  const seen = new Set();
  for (const m of messages) {
    if (!seen.has(m)) {
      uniq.push(m);
      seen.add(m);
    }
  }
  messages.length = 0;
  messages.push(...uniq);

  const totalLen = messages.reduce((a, b) => a + b.length, 0);
  if (totalLen > 160) return false;

  const joined = messages.join('\n');
  const banned = ['首先', '其次', '总的来说', '综上', '建议', '步骤', '方案', '总结', '你可以尝试'];
  if (banned.some(w => joined.includes(w))) return false;

  return true;
}

async function sendBubbles(token, userId, messages) {
  for (let i = 0; i < messages.length; i++) {
    await sendMessageWithToken(token, userId, messages[i]);
    if (i !== messages.length - 1) {
      await sleep(randomInt(350, 1200));
    }
  }
}

/** ===================== SiliconFlow：embedding + chat ===================== */
async function getEmbedding(text) {
  const res = await fetch('https://api.siliconflow.cn/v1/embeddings', {
    method: 'POST',
    headers: { Authorization: `Bearer ${SILICONFLOW_KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: 'BAAI/bge-m3', input: text.slice(0, 2000), encoding_format: 'float' })
  });
  const data = await res.json();
  if (!data?.data?.[0]?.embedding) {
    throw new Error(`Embedding failed: ${JSON.stringify(data).slice(0, 400)}`);
  }
  return data.data[0].embedding;
}

/** ===================== 本地“回复库”向量检索（纯JS暴力版） ===================== */
/**
 * 需要在仓库里放：reply_index/vectors.json
 * 格式：[{ text: "...", embedding: [float...1024] }, ...]
 */
let _replyVectors = null; // [{text, embedding}]
function loadReplyVectorsOnce() {
  if (_replyVectors) return;
  const p = path.join(process.cwd(), 'reply_index', 'vectors.json');
  const raw = fs.readFileSync(p, 'utf-8');
  const arr = JSON.parse(raw);
  _replyVectors = Array.isArray(arr) ? arr : [];
  console.log('[LOCAL VECTORS] loaded', _replyVectors.length);
}

function dot(a, b) {
  let s = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) s += a[i] * b[i];
  return s;
}
function norm(a) {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * a[i];
  return Math.sqrt(s) || 1;
}
function cosineSim(a, b) {
  return dot(a, b) / (norm(a) * norm(b));
}

function searchRepliesLocal(queryEmbedding, k = 8) {
  loadReplyVectorsOnce();
  if (!_replyVectors.length) return [];

  // 维护一个小 topK（数组长度<=k）
  const top = [];
  for (let i = 0; i < _replyVectors.length; i++) {
    const item = _replyVectors[i];
    const emb = item.embedding;
    if (!emb || !Array.isArray(emb)) continue;

    const score = cosineSim(queryEmbedding, emb);

    if (top.length < k) {
      top.push({ score, content: item.text });
      if (top.length === k) top.sort((x, y) => x.score - y.score); // 小到大
    } else if (score > top[0].score) {
      top[0] = { score, content: item.text };
      top.sort((x, y) => x.score - y.score);
    }
  }

  // 由高到低返回
  return top.sort((a, b) => b.score - a.score).map(x => ({ content: x.content }));
}

/** ===================== 生成多气泡短回复 ===================== */
async function generateReplyBubbles(question, exemplars) {
  const exText = (exemplars && exemplars.length > 0)
    ? exemplars.map((c, i) => `示例${i + 1}：\n${c.content}`).join('\n\n')
    : '';

  const prompt = `你在企业微信里扮演“吹雨本人”和对方聊天（闲聊为主，不要像客服/老师）。
必须输出严格JSON，格式如下：
{"messages":["...","..."]}

硬规则：
- 默认 1~3 条气泡；每条尽量短（5~20字常见），最多60字。
- 不要长文，不要说教，不要写“首先/其次/建议/综上/方案/步骤/总结”。
- 不要输出任何占位符或动作描述：例如[动画表情]/[图片]/[语音]/[xx]。
- 不确定就直说“不太确定/不知道”，不要装懂。
- 不要连续复读同一句。

下面是“吹雨以前类似场景怎么回”的示例（只学语气和节奏，不要照抄内容）：
${exText || '（无）'}

对方发来：
${question}

现在请按吹雨的微信风格回复（只输出JSON，不要输出其它任何文字）。`;

  const res = await fetch('https://api.siliconflow.cn/v1/chat/completions', {
    method: 'POST',
    headers: { Authorization: `Bearer ${SILICONFLOW_KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: 'deepseek-ai/DeepSeek-V3',
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 256,
      temperature: 0.85
    })
  });

  const data = await res.json();
  const raw = data?.choices?.[0]?.message?.content ?? '';
  return parseMessagesFromModel(raw);
}

/** ===================== Vercel handler ===================== */
export default async function handler(req, res) {
  const urlObj = new URL(req.url, `https://${req.headers.host}`);
  const p = urlObj.searchParams;

  if (req.method === 'GET') {
    const sig = p.get('msg_signature') || '';
    const ts  = p.get('timestamp') || '';
    const nc  = p.get('nonce') || '';
    const es  = p.get('echostr') || '';
    if (verifySignature(sig, ts, nc, es)) {
      res.status(200).send(wxDecrypt(es));
    } else {
      res.status(403).send('Forbidden');
    }
    return;
  }

  if (req.method === 'POST') {
    const sig = p.get('msg_signature') || '';
    const ts  = p.get('timestamp') || '';
    const nc  = p.get('nonce') || '';

    const body = await getRawBody(req);
    const encrypted = getXmlValue(body, 'Encrypt');
    if (!encrypted) { res.status(400).send('Bad Request'); return; }
    if (!verifySignature(sig, ts, nc, encrypted)) { res.status(403).send('Forbidden'); return; }

    const xmlStr  = wxDecrypt(encrypted);
    const msgType = getXmlValue(xmlStr, 'MsgType');
    const userId  = getXmlValue(xmlStr, 'FromUserName');
    const content = getXmlValue(xmlStr, 'Content').trim();

    console.log('[MSG] userId:', userId, 'content:', content);

    if (msgType === 'text' && userId && content) {
      waitUntil((async () => {
        const token = await getAccessToken();

        // 可选：先发一句“在打字”式确认
        // await sendMessageWithToken(token, userId, 'emm');

        let messages = [];
        try {
          console.log('[REPLY-RAG] 开始');
          const embedding = await getEmbedding(content);
          console.log('[REPLY-RAG] embedding完成');

          const exemplars = searchRepliesLocal(embedding, 8);
          console.log('[REPLY-RAG] 本地检索完成，exemplars:', exemplars?.length);

          messages = await generateReplyBubbles(content, exemplars);
          console.log('[REPLY-RAG] 生成完成 messages:', messages);
        } catch (err) {
          console.error('[REPLY-RAG ERROR]', err?.message || err);
        }

        if (!qualityGuard(messages)) {
          messages = ['我有点懵,你再说清楚点'];
        }

        await sendBubbles(token, userId, messages);
      })());
    }

    res.status(200).send('success');
    return;
  }

  res.status(405).send('Method Not Allowed');
}