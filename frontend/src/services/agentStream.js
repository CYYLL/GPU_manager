// SSE over fetch POST (EventSource only supports GET, and /api/agent/chat/stream
// is a POST). Parses `data: {json}\n\n` frames incrementally from the body reader.
//
// Server event contract (agent_loop.run_agent_stream):
//   {"event":"text","delta":str}
//   {"event":"tool_use","tool":str,"input":obj}
//   {"event":"tool_result","tool":str,"ok":bool,"result":str}
//   {"event":"done","reply":str,"tool_trace":[...]}

const BASE = process.env.REACT_APP_API_URL || '';

function authHeaders(extra = {}) {
  const token = localStorage.getItem('token');
  const tokenType = localStorage.getItem('tokenType') || 'bearer';
  const headers = { 'Content-Type': 'application/json', ...extra };
  if (token) headers.Authorization = `${tokenType} ${token}`;
  return headers;
}

/**
 * Send one chat message and consume the SSE stream.
 * @param {object} opts
 *   message: string
 *   onText(delta), onToolUse({tool,input}), onToolResult({tool,ok,result})
 *   onDone(reply, toolTrace), onError(err), onClose()
 *   signal: optional AbortSignal
 */
export async function streamChat(opts) {
  const { message, onText, onToolUse, onToolResult, onDone, onError, onClose, signal } = opts;
  let res;
  try {
    res = await fetch(`${BASE}/api/agent/chat/stream`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ message }),
      signal,
    });
  } catch (err) {
    onError && onError(err);
    onClose && onClose();
    return;
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (e) { /* non-json body */ }
    onError && onError(new Error(detail));
    onClose && onClose();
    return;
  }

  if (!res.body) {
    onError && onError(new Error('浏览器不支持流式响应'));
    onClose && onClose();
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);
        if (!frame) continue;
        dispatchFrame(frame, { onText, onToolUse, onToolResult, onDone, onError });
      }
    }
    if (buffer.trim()) dispatchFrame(buffer.trim(), { onText, onToolUse, onToolResult, onDone, onError });
  } catch (err) {
    if (err.name !== 'AbortError') onError && onError(err);
  } finally {
    onClose && onClose();
  }
}

function dispatchFrame(frame, { onText, onToolUse, onToolResult, onDone, onError }) {
  if (!frame.startsWith('data:')) return; // ignore comments/keep-alives
  const payload = frame.slice(5).trim();
  if (!payload) return;
  let ev;
  try {
    ev = JSON.parse(payload);
  } catch (e) {
    return;
  }
  switch (ev.event) {
    case 'text':
      if (ev.delta) onText && onText(ev.delta);
      break;
    case 'tool_use':
      onToolUse && onToolUse({ tool: ev.tool, input: ev.input });
      break;
    case 'tool_result':
      onToolResult && onToolResult({ tool: ev.tool, ok: ev.ok, result: ev.result });
      break;
    case 'done':
      onDone && onDone(ev.reply, ev.tool_trace || []);
      break;
    default:
      break;
  }
}

export { authHeaders };
