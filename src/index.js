'use strict';

const REPLY_TEXT = '对不起做不到。';
const RETRY_DELAY_MS = 3000;
const ACTION_TIMEOUT_MS = 10000;

const wsUrl = (process.env.NAPCAT_WS_URL || 'ws://127.0.0.1:3001').trim();
const wsToken = (process.env.NAPCAT_WS_TOKEN || '').trim();

let socket = null;
let reconnectTimer = null;
/** @type {Map<string, {resolve: Function, reject: Function, timer: NodeJS.Timeout, desc: string}>} */
const pendingActions = new Map();

function log(...args) {
  console.log(new Date().toISOString(), ...args);
}

function buildTargetUrl() {
  if (!wsToken) return wsUrl;
  const url = new URL(wsUrl);
  url.searchParams.set('access_token', wsToken);
  return url.toString();
}

function maskUrl(raw) {
  try {
    const url = new URL(raw);
    if (url.searchParams.has('access_token')) url.searchParams.set('access_token', '***');
    return url.toString();
  } catch {
    return raw;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, RETRY_DELAY_MS);
}

function rejectAllPending(err) {
  for (const [, entry] of pendingActions) {
    clearTimeout(entry.timer);
    entry.reject(err);
  }
  pendingActions.clear();
}

function connect() {
  if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
    return;
  }

  const target = buildTargetUrl();
  log(`正在连接 NapCat（${maskUrl(target)}）...`);

  const current = new WebSocket(target);
  socket = current;

  current.onopen = () => {
    log('已连接 NapCat OneBot WebSocket。');
  };

  current.onmessage = (event) => {
    handleIncoming(event.data).catch((err) => {
      log('处理消息出错：', err && err.message ? err.message : err);
    });
  };

  current.onerror = () => {
    // 连接失败/断开时随后会触发 onclose，由 onclose 统一安排重连。
  };

  current.onclose = () => {
    if (socket !== current) return;
    socket = null;
    log('与 NapCat 的连接已断开，稍后自动重连...');
    rejectAllPending(new Error('WebSocket 连接已断开'));
    scheduleReconnect();
  };
}

async function handleIncoming(data) {
  if (typeof data !== 'string') return;

  let payload;
  try {
    payload = JSON.parse(data);
  } catch {
    return;
  }
  if (!payload || typeof payload !== 'object') return;

  // 动作调用响应：按 echo 找到对应的发送请求。
  if (payload.echo !== undefined && pendingActions.has(String(payload.echo))) {
    const entry = pendingActions.get(String(payload.echo));
    pendingActions.delete(String(payload.echo));
    clearTimeout(entry.timer);
    if (payload.status === 'ok' && payload.retcode === 0) {
      entry.resolve(payload);
    } else {
      entry.reject(new Error(`${entry.desc} 失败：${JSON.stringify(payload)}`));
    }
    return;
  }

  // 只处理群聊消息事件。
  if (payload.post_type !== 'message' || payload.message_type !== 'group') return;

  const selfId = String(payload.self_id ?? '');
  const userId = String(payload.user_id ?? '');
  const groupId = payload.group_id;

  // 忽略机器人自己发出的消息，避免自触发。
  if (!selfId || userId === selfId) return;
  if (groupId === undefined || groupId === null) return;

  if (!isAtSelf(payload, selfId)) return;

  log(`群 ${groupId} 收到 @机器人 消息（发送者 ${userId}），回复：${REPLY_TEXT}`);
  await sendAction('send_group_msg', {
    group_id: groupId,
    message: REPLY_TEXT
  });
}

function isAtSelf(payload, selfId) {
  const segments = payload.message;
  if (Array.isArray(segments)) {
    return segments.some(
      (seg) =>
        seg &&
        seg.type === 'at' &&
        seg.data &&
        String(seg.data.qq) === selfId
    );
  }

  // 兜底：若 NapCat 配置了字符串消息格式，则解析 raw_message 中的 CQ 码。
  const raw = String(payload.raw_message ?? '');
  if (!raw) return false;
  const atPattern = /\[CQ:at(?:,([^\]]*))?\]/g;
  for (const match of raw.matchAll(atPattern)) {
    const params = match[1] ? match[1].split(',') : [];
    for (const part of params) {
      const eq = part.indexOf('=');
      if (eq === -1) continue;
      const key = part.slice(0, eq).trim();
      const value = part.slice(eq + 1).trim();
      if (key === 'qq' && value === selfId) return true;
    }
  }
  return false;
}

function sendAction(action, params) {
  return new Promise((resolve, reject) => {
    const current = socket;
    if (!current || current.readyState !== WebSocket.OPEN) {
      reject(new Error('WebSocket 未连接，无法发送动作'));
      return;
    }

    const echo = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    const timer = setTimeout(() => {
      pendingActions.delete(echo);
      reject(new Error(`${action} 等待响应超时`));
    }, ACTION_TIMEOUT_MS);

    pendingActions.set(echo, {
      resolve,
      reject,
      timer,
      desc: action
    });

    current.send(JSON.stringify({ action, params, echo }));
  });
}

connect();
