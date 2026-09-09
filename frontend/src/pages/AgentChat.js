import React, { useState, useEffect, useRef, useCallback } from 'react';
import api from '../services/api';
import { streamChat } from '../services/agentStream';
import { useNavigate } from 'react-router-dom';
import { useMode } from '../context/ModeContext';
import AppHeader from '../components/AppHeader';

let nextId = 1;
// 模块级标记（同一 SPA 会话内路由切走/切回组件不会重置）：记录"离开 Agent 页时
// 是否带着一条进行中的请求"。回来时据此判断是否需要与后端对账——只有本页自己
// 离开前发过中断的请求才算 leftover，避免把别的 tab 正在正常执行的请求误显示为"正在停止"。
let leftWhileRunning = false;

// 工具结果默认折叠展示，可展开看全部 —— 有大小上限但不破坏语义：
// 上限作用于“展示层”，切分只发生在行/词边界，绝不在一个数字/词中间切断；
// 完整结果始终保存在 chip 里，点“展开”即可看全，因此被折叠的信息没有丢失。
const COLLAPSED_LINES = 8;      // 折叠时最多展示的行数
const MAX_PREVIEW_CHARS = 800;  // 折叠预览的总字符上限（超出部分按行/词截断）
const LEFTOVER_POLL_MS = 1500;  // 回到页面、上一条仍在收尾时，轮询后端 gate 状态的间隔

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
  const [cancelling, setCancelling] = useState(false); // 已点"停止"、等后端返回 请求中断
  const [error, setError] = useState('');
  const [leftover, setLeftover] = useState(false); // 上一条(离开页时被中断的)请求仍在后端收尾
  const listRef = useRef(null);
  // 卸载清理也要知道"此刻是否仍有请求在跑"：state 在清理函数里读不到最新值，用 ref 镜像。
  const sendingRef = useRef(false);

  useEffect(() => {
    const u = localStorage.getItem('user');
    if (u) setUser(JSON.parse(u));
  }, []);

  const scrollBottom = useCallback(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, []);

  useEffect(() => { scrollBottom(); }, [messages, leftover, scrollBottom]);

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

  // 切换页面（卸载本组件）时，若仍有请求在执行，异步发出中断。与"停止"按钮
  // 同策略：不断开 SSE，让后端在下一个轮次/回复边界返回 done("请求中断")，
  // 落库并释放单飞 gate —— 否则请求会在这页无人看管时继续执行到自然结束，
  // 回来 loadSession 却看不到任何进行中的痕迹（后台在跑、界面没有请求）。
  // 卸载后回调里对已卸载组件的 setState 是空操作，无副作用。
  useEffect(() => {
    return () => {
      if (sendingRef.current) {
        leftWhileRunning = true; // 记住本页离开时带着一条进行中的请求，回来要对账
        api.post('/api/agent/cancel').catch(() => {});
      }
    };
  }, []);

  // 回到 Agent 页时与后端对账（仅限"本页离开时发过中断"的场景）：离开时已异步
  // 发 cancel，但中断要到下一个轮次/回复边界才落定并释放单飞 gate。若用户在收尾
  // 完成前就返回，会话历史里只有那条用户消息（assistant 的 请求中断 尚未落库），
  // 直接 loadSession 会误显为"闲置"——正是最初的问题。这里轮询 /api/agent/status：
  // gate 未释放 → 保持"正在停止…"状态并继续等；gate 释放（_save 先于 release
  // 执行）→ 重载会话，把落库的 请求中断 收尾回复显示出来，再解除中断中状态。
  useEffect(() => {
    if (mode !== 'llm') return;
    if (!leftWhileRunning) return; // 没有"离开时中断的请求"，直接闲置，无需对账
    let disposed = false;
    let timer = null;
    const poll = async () => {
      if (disposed) return;
      let running = false;
      try {
        const res = await api.get('/api/agent/status');
        running = !!(res.data && res.data.running);
      } catch (err) {
        // status 端点瞬时失败：按闲置结束轮询，避免卡在中断态或无限请求。
      }
      if (disposed) return;
      if (running) {
        setLeftover(true);
        timer = setTimeout(poll, LEFTOVER_POLL_MS);
      } else {
        // running=false：收尾已落库（_save 先于 gate 释放）。挂载时的 loadSession
        // 可能抢在落库前返回了旧历史，这里无条件重载一次收敛到最终会话——既覆盖
        // "恰好卡在收尾完成瞬间"的竞态，也让 请求中断 收尾回复显示出来。
        setLeftover(false);
        leftWhileRunning = false;
        loadSession();
      }
    };
    poll();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
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
    if (!text || sending || leftover) return;
    setInput('');
    setError('');
    setSending(true);
    sendingRef.current = true;
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
        sendingRef.current = false;
        setCancelling(false);
        patchLastAssistant((a) => (a.streaming ? { ...a, streaming: false } : a));
      },
    });
  };

  // 停止：只向后端发 cancel，绝不 abort fetch/SSE —— 后端在中断点返回
  // done("请求中断")，继续读流才能收到并展示这条收尾回复（见 agent_loop 的
  // cancel_check / INTERRUPT_REPLY）。若请求恰在自然结束瞬间被点停止，
  // cancel 幂等返回 200，无副作用；onDone/onClose 照常落定气泡。
  const handleStop = async () => {
    if (!sending || cancelling) return;
    setCancelling(true);
    setError('');
    try {
      await api.post('/api/agent/cancel');
      // 不在此改 sending：等后端 done("请求中断") → onClose 统一收尾。
    } catch (err) {
      setError(err.response?.data?.detail || '停止请求发送失败，可重试');
      setCancelling(false); // 允许再次点击停止
    }
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
                disabled={sending || leftover || messages.length === 0}>
                清空会话
              </button>
            </div>
            <p style={{ fontSize: 12, color: '#aaa', marginTop: 4 }}>
              中：我能查询/创建/启动/停止/删除/重建你的容器，设置清理保护。删除容器会销毁容器本体（工作区保留）。
            </p>

            <div className="chat-list" ref={listRef}>
              {messages.length === 0 && !sending && !leftover && (
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
              {/* 上一条请求离开页时被中断、后端仍在收尾：先给一条进行中气泡占位，
                  等 gate 释放后 loadSession 会把它换成落库的 请求中断 真实回复。 */}
              {leftover && !sending && (
                <div className="chat-msg chat-assistant">
                  <div className="chat-bubble">
                    <div style={{ whiteSpace: 'pre-wrap' }}>正在停止上一条请求…</div>
                    <span className="chat-typing" />
                  </div>
                </div>
              )}
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
                disabled={sending || leftover}
              />
              {sending || leftover ? (
                <button className="btn btn-danger" onClick={handleStop} disabled={cancelling || leftover}>
                  {cancelling || leftover ? '正在停止…' : '停止'}
                </button>
              ) : (
                <button className="btn btn-primary" onClick={handleSend} disabled={!input.trim()}>
                  发送
                </button>
              )}
            </div>
          </div>
      </div>
    </div>
  );
};

export default AgentChat;
