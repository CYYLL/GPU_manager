import React, { useState, useEffect, useRef, useCallback } from 'react';
import api from '../services/api';
import { streamChat } from '../services/agentStream';
import { useNavigate } from 'react-router-dom';
import { useMode } from '../context/ModeContext';
import AppHeader from '../components/AppHeader';

let nextId = 1;

const AgentChat = () => {
  const { mode, loading: modeLoading } = useMode();
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
          const summary = (result || '').replace(/\s+/g, ' ').trim();
          const chip = {
            key: nextId++,
            tool,
            status,
            summary: summary.length > 160 ? summary.slice(0, 160) + '…' : summary,
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
    if (!modeLoading && mode !== 'llm') navigate('/', { replace: true });
  }, [mode, modeLoading, navigate]);

  if (modeLoading || mode !== 'llm') return null; // 解析模式中 / 传统模式 → 定位到主页

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
                        {m.chips.map((c) => (
                          <div key={c.key} className={`tool-chip tool-${c.status}`}>
                            <span className="tool-chip-icon">
                              {c.status === 'running' ? '⋯' : c.status === 'ok' ? '✓' : '✗'}
                            </span>
                            <span className="tool-chip-name">{c.tool}</span>
                            {c.summary && <span className="tool-chip-summary">{c.summary}</span>}
                          </div>
                        ))}
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
