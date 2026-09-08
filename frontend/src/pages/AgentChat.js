import React, { useState, useEffect, useRef, useCallback } from 'react';
import api from '../services/api';
import { streamChat } from '../services/agentStream';
import { useNavigate } from 'react-router-dom';
import { useMode } from '../context/ModeContext';
import AppHeader from '../components/AppHeader';

let nextId = 1;

// 工具结果默认折叠展示，可展开看全部 —— 有大小上限但不破坏语义：
// 上限作用于“展示层”，切分只发生在行/词边界，绝不在一个数字/词中间切断；
// 完整结果始终保存在 chip 里，点“展开”即可看全，因此被折叠的信息没有丢失。
const COLLAPSED_LINES = 8;      // 折叠时最多展示的行数
const MAX_PREVIEW_CHARS = 800;  // 折叠预览的总字符上限（超出部分按行/词截断）

// 在给定预算内，从「能完整放下多少行」开始，最后一行若放不下则退化到词边界截断。
function makePreview(lines) {
  const n = Math.min(lines.length, COLLAPSED_LINES);
  let budget = MAX_PREVIEW_CHARS;
  const kept = [];
  for (let i = 0; i < n; i++) {
    const line = lines[i];
    if (line.length <= budget) {
      kept.push(line);
      budget -= line.length + 1;  // +1 for the '\n' separator
      if (budget < 0) break;
      continue;
    }
    // 单行超预算：只在词边界截断，绝不把一个值劈成两半。
    let cut = line.slice(0, Math.max(budget, 0));
    const lastSpace = cut.lastIndexOf(' ');
    if (lastSpace > 0) cut = cut.slice(0, lastSpace);
    kept.push(cut + ' …');
    break;
  }
  return kept.join('\n');
}

function ToolChip({ c }) {
  const [expanded, setExpanded] = useState(false);
  const lines = (c.summary || '').split('\n');
  const preview = makePreview(lines);
  // 存在比折叠展示更多的内容，或单行被截断 → 需要“展开/收起”
  const collapsible =
    expanded || lines.length > COLLAPSED_LINES || preview.length < (c.summary || '').length;
  const hidden = lines.length - preview.split('\n').length;
  return (
    <div className={`tool-chip tool-${c.status}`}>
      <span className="tool-chip-icon">
        {c.status === 'running' ? '⋯' : c.status === 'ok' ? '✓' : '✗'}
      </span>
      <span className="tool-chip-name">{c.tool}</span>
      {c.summary && (
        <span className="tool-chip-summary">{expanded ? lines.join('\n') : preview}</span>
      )}
      {collapsible && (
        <button type="button" className="tool-chip-more" onClick={() => setExpanded((e) => !e)}>
          {expanded ? '收起' : `展开（${hidden > 0 ? `还有 ${hidden} 行，` : ''}查看全文）`}
        </button>
      )}
    </div>
  );
}

const AgentChat = () => {
  const { mode, confirmed, loading: modeLoading } = useMode();
  const navigate = useNavigate();
  const [user, setUser] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const listRef = useRef(null);

  useEffect(() => {
    const u = localStorage.getItem('user');
    if (u) setUser(JSON.parse(u));
  }, []);

  const scrollBottom = useCallback(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, []);

  useEffect(() => { scrollBottom(); }, [messages, scrollBottom]);

  const loadSession = useCallback(async () => {
    try {
      const res = await api.get('/api/agent/session');
      const rows = (res.data?.messages || []).map((m) => ({
        id: nextId++,
        role: m.role === 'user' ? 'user' : 'assistant',
        content: m.content || '',
        chips: [],
        streaming: false,
      }));
      setMessages(rows);
    } catch (err) {
      setError(err.response?.data?.detail || '加载会话失败');
    }
  }, []);

  useEffect(() => {
    if (mode === 'llm') loadSession();
  }, [mode, loadSession]);

  const patchLastAssistant = useCallback((fn) => {
    setMessages((prev) => {
      const next = prev.slice();
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].role === 'assistant') { next[i] = { ...next[i], ...fn(next[i]) }; break; }
      }
      return next;
    });
  }, []);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput('');
    setError('');
    setSending(true);
    setMessages((prev) => [
      ...prev,
      { id: nextId++, role: 'user', content: text, chips: [], streaming: false },
      { id: nextId++, role: 'assistant', content: '', chips: [], streaming: true },
    ]);

    streamChat({
      message: text,
      onText: (delta) => {
        patchLastAssistant((a) => ({ ...a, content: a.content + delta }));
      },
      onToolUse: ({ tool }) => {
        patchLastAssistant((a) => ({
          ...a,
          chips: [...a.chips, { key: nextId++, tool, status: 'running', summary: '' }],
        }));
      },
      onToolResult: ({ tool, ok, result }) => {
        patchLastAssistant((a) => {
          const chips = a.chips.slice();
          // 找到该工具最后一个"执行中"chip 并落定；找不到则补一条
          let idx = -1;
          for (let i = chips.length - 1; i >= 0; i--) {
            if (chips[i].tool === tool && chips[i].status === 'running') { idx = i; break; }
          }
          const status = ok ? 'ok' : 'fail';
          // 完整保留工具结果（含换行、不截断）——展示层按行折叠，展开时仍可看全。
          // 若这里截断，折叠后再展开也无法恢复被切掉的语义。
          const summary = (result || '').trim();
          const chip = {
            key: nextId++,
            tool,
            status,
            summary,
          };
          if (idx >= 0) chips[idx] = chip; else chips.push(chip);
          return { ...a, chips };
        });
      },
      onDone: (reply) => {
        patchLastAssistant((a) => ({ ...a, content: reply || a.content, streaming: false }));
      },
      onError: (err) => {
        setError(err.message || String(err));
        patchLastAssistant((a) => ({ ...a, streaming: false }));
      },
      onClose: () => {
        setSending(false);
        patchLastAssistant((a) => (a.streaming ? { ...a, streaming: false } : a));
      },
    });
  };

  const handleClear = async () => {
    if (!window.confirm('清空当前会话历史？')) return;
    try {
      await api.delete('/api/agent/session');
      setMessages([]);
    } catch (err) {
      setError(err.response?.data?.detail || '清空失败');
    }
  };

  // 传统模式：不展示 Agent/LLM 说明卡，直接回到主页（/）。顶栏 ModeToggle 可随时切回 LLM 模式后再进入。
  useEffect(() => {
    if (!modeLoading && (mode !== 'llm' || !confirmed)) navigate('/', { replace: true });
  }, [mode, modeLoading, confirmed, navigate]);

  if (modeLoading || mode !== 'llm' || !confirmed) return null; // 解析/传统/未确认 → 定位主页

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
          <div className="card chat-card">
            <div className="chat-head">
              <h2 style={{ margin: 0 }}>Agent 助手</h2>
              <button className="btn btn-secondary" onClick={handleClear}
                disabled={sending || messages.length === 0}>
                清空会话
              </button>
            </div>
            <p style={{ fontSize: 12, color: '#aaa', marginTop: 4 }}>
              中：我能查询/创建/启动/停止/删除/重建你的容器，设置清理保护。删除容器会销毁容器本体（工作区保留）。
            </p>

            <div className="chat-list" ref={listRef}>
              {messages.length === 0 && !sending && (
                <p className="chat-empty">还没有对话。输入一个指令开始，例如"我有哪些容器？"</p>
              )}
              {messages.map((m) => (
                <div key={m.id} className={`chat-msg ${m.role === 'user' ? 'chat-user' : 'chat-assistant'}`}>
                  <div className="chat-bubble">
                    <div style={{ whiteSpace: 'pre-wrap' }}>{m.content}</div>
                    {m.streaming && <span className="chat-typing" />}
                    {m.role === 'assistant' && m.chips.length > 0 && (
                      <div className="chat-chips">
                        {m.chips.map((c) => <ToolChip key={c.key} c={c} />)}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {error && <div className="chat-error">{error}</div>}
            <div className="chat-input-row">
              <textarea
                className="chat-input"
                rows={2}
                placeholder="描述你的需求，例如：帮我启动一个 basic 镜像、2 张卡的容器"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
                }}
                disabled={sending}
              />
              <button className="btn btn-primary" onClick={handleSend} disabled={sending || !input.trim()}>
                {sending ? '执行中…' : '发送'}
              </button>
            </div>
          </div>
      </div>
    </div>
  );
};

export default AgentChat;
