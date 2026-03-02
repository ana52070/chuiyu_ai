import crypto from 'crypto';
import { waitUntil } from '@vercel/functions';

const CORP_ID     = process.env.WXWORK_CORP_ID;
const AGENT_ID    = process.env.WXWORK_AGENT_ID;
const CORP_SECRET = process.env.WXWORK_SECRET;
const WX_TOKEN    = process.env.WXWORK_TOKEN;
const WX_AES_KEY  = Buffer.from((process.env.WXWORK_AES_KEY || '') + '=', 'base64');

const SUPABASE_URL    = process.env.SUPABASE_URL;
const SUPABASE_KEY    = process.env.SUPABASE_KEY;
const SILICONFLOW_KEY = process.env.SILICONFLOW_KEY;

// VPS nginx 代理地址，所有发往企业微信的请求都走这里
const VPS_PROXY = 'http://49.233.85.74:8080';

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

// === 过滤不可执行占位符 / 方括号表情 ===
const BAD_TOKENS = new Set([
  '[动画表情]', '[表情]', '[图片]', '[语音]', '[视频]', '[文件]',
  '[位置]', '[名片]', '[链接]', '[红包]', '[转账]'
]);

function cleanBubble(s) {
  if (!s) return '';
  let t = String(s).trim();
  if (!t) return '';
  if (BAD_TOKENS.has(t)) return '';
  // 去掉类似 [捂脸] 这种方括号表情（你也可以改成映射 emoji）
  t = t.replace(/\[[^\]]{1,12}\]/g, '').trim();
  if (!t) return '';
  // 限制单条长度（防止变长文）
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
  const totalLen = messages.reduce((a, b) => a + b.length, 0);
  if (totalLen > 160) return false;

  const joined = messages.join('\n');
  // 反 AI 味硬挡（你可以继续加）
  const banned = ['首先', '其次', '总的来说', '综上', '建议', '步骤', '方案', '总结', '你可以尝试'];
  if (banned.some(w => joined.includes(w))) return false;

  return true;
}

async function getEmbedding(text) {
  const res = await fetch('https://api.siliconflow.cn/v1/embeddings', {
    method: 'POST',
    headers: { Authorization: `Bearer ${SILICONFLOW_KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: 'BAAI/bge-m3', input: text.slice(0, 2000), encoding_format: 'float' })
  });
  const data = await res.json();
  return data.data[0].embedding;
}

// === 从“你的历史回复库”检索 exemplars ===
// 需要 Supabase RPC：match_replies（或你复用 match_documents 也行）
async function searchReplies(embedding) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/rpc/match_replies`, {
    method: 'POST',
    headers: { apikey: SUPABASE_KEY, Authorization: `Bearer ${SUPABASE_KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ query_embedding: embedding, match_count: 8, match_threshold: 0.5 })
  });
  return res.json();
}

async function generateReplyBubbles(question, exemplars) {
  const exText = (exemplars && exemplars.length > 0)
    ? exemplars.map((c, i) => `示例${i + 1}：\n${c.content}`).join('\n\n')
    : '';

  // 核心：强制输出 JSON messages[]，短、碎、像微信，别讲道理
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
      temperature: 0.8
    })
  });
  const data = await res.json();
  const raw = data?.choices?.[0]?.message?.content ?? '';
  return parseMessagesFromModel(raw);
}

async function sendBubbles(token, userId, messages) {
  // 逐条发送，模拟人类断断续续
  for (let i = 0; i < messages.length; i++) {
    await sendMessageWithToken(token, userId, messages[i]);
    // 最后一条就不等太久
    if (i !== messages.length - 1) {
      await sleep(randomInt(350, 1200));
    }
  }
}

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

        // 可选：先发一句“在打字”式的确认（你也可以删掉）
        await sendMessageWithToken(token, userId, '等我想想😂');

        let messages = [];
        try {
          console.log('[REPLY-RAG] 开始');
          const embedding = await getEmbedding(content);
          console.log('[REPLY-RAG] embedding完成');

          const exemplars = await searchReplies(embedding);
          console.log('[REPLY-RAG] 检索完成，exemplars:', exemplars?.length);

          // 生成多条短回复（你也可以后续加 n_cand 多候选+评分，这里先单次生成）
          messages = await generateReplyBubbles(content, exemplars);
          console.log('[REPLY-RAG] 生成完成 messages:', messages);

        } catch (err) {
          console.error('[REPLY-RAG ERROR]', err?.message || err);
        }

        // 质量挡板：不合格就用安全短回复
        if (!qualityGuard(messages)) {
          messages = ['我有点懵😂你再说清楚点'];
        }

        // 逐条发送
        await sendBubbles(token, userId, messages);
      })());
    }

    res.status(200).send('success');
    return;
  }

  res.status(405).send('Method Not Allowed');
}