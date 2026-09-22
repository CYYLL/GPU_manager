import { apiErrorText } from '../services/errorMessages';
import React, { useState, useEffect, useRef, useCallback } from 'react';
import api from '../services/api';
import { streamChat } from '../services/agentStream';
import { useNavigate } from 'react-router-dom';
import { useMode } from '../context/ModeContext';
import AppHeader from '../components/AppHeader';

let nextId = 1;
// 页面切换不取消后台请求；返回时用此标记恢复对运行状态的跟踪。
let leftWhileRunning = false;

const LEFTOVER_POLL_MS = 1500;
const toolLabels = {
  list_containers: '查询容器', get_container_status: '查询容器状态',
  get_gpu_status: '查询 GPU 状态', get_disk_status: '查询磁盘状态',
  list_images: '查询镜像预设', set_image_preset: '更新镜像预设',
  list_local_images: '查询本地镜像', inspect_image: '分析镜像',
  search_hub_images: '搜索镜像', pull_hub_image: '拉取镜像',
  delete_local_image: '删除镜像', set_container_protection: '设置容器保护',
  check_gpu_quota: '查询 GPU 配额', set_user_quota: '设置用户配额',
  list_users: '查询用户', delete_user: '删除用户',
  create_container: '创建容器', repair_container_ssh: '修复 SSH 登录',
  start_container: '启动容器', stop_container: '停止容器',
  delete_container: '删除容器', rebuild_container: '重建容器',
};

function ToolChip({ c }) {
  return (
    <div className={`tool-chip tool-${c.status}`}>
      <span className="tool-chip-icon">
        {c.status === 'running' ? '⋯' : c.status === 'ok' ? '✓' : '✗'}
      </span>
      <span className="tool-chip-name">{toolLabels[c.tool] || '工具调用'}</span>
      <span>{c.status === 'running' ? '执行中' : c.status === 'ok' ? '成功' : '失败'}</span>
    </div>
  );
}

const settleRunningChips = (chips) => chips.map((chip) => (
  chip.status === 'running' ? { ...chip, status: 'fail' } : chip
));

const AgentChat = () => {
  const { mode, confirmed, loading: modeLoading } = useMode();
  const navigate = useNavigate();
  const [user, setUser] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [cancelling, setCancelling] = useState(false); // 已点"停止"、等后端返回 请求中断
  const [error, setError] = useState('');
  const [leftover, setLeftover] = useState(false); // 离开页面后仍在后台执行的请求
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
        chips: (m.tool_calls || []).map((call) => ({
          key: nextId++,
          tool: call.tool,
          status: call.ok ? 'ok' : 'fail',
        })),
        streaming: false,
      }));
      setMessages(rows);
    } catch (err) {
      setError(apiErrorText(err, '加载会话失败'));
    }
  }, []);

  useEffect(() => {
    if (mode === 'llm') loadSession();
  }, [mode, loadSession]);

  // 页面切换仅记录任务仍在进行，不发送 cancel；只有用户按"停止"才中断。
  useEffect(() => {
    return () => {
      if (sendingRef.current) {
        leftWhileRunning = true;
      }
    };
  }, []);

  // 回到 Agent 页时查询后台请求；完成后重载已经落库的工具结果和回复。
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
        // status 端点瞬时失败：按闲置结束轮询，避免无限请求。
      }
      if (disposed) return;
      if (running) {
        setLeftover(true);
        timer = setTimeout(poll, LEFTOVER_POLL_MS);
      } else {
        // _save 先于 gate 释放；再次加载以覆盖挂载时可能读取的旧历史。
        setLeftover(false);
        setCancelling(false);
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
          chips: [...a.chips, { key: nextId++, tool, status: 'running' }],
        }));
      },
      onToolProgress: ({ tool, percent }) => {
        if (tool === 'pull_hub_image') {
          patchLastAssistant((a) => ({ ...a, pullProgress: percent }));
        }
      },
      onToolResult: ({ tool, ok }) => {
        patchLastAssistant((a) => {
          const chips = a.chips.slice();
          // 找到该工具最后一个"执行中"chip 并落定；找不到则补一条
          let idx = -1;
          for (let i = chips.length - 1; i >= 0; i--) {
            if (chips[i].tool === tool && chips[i].status === 'running') { idx = i; break; }
          }
          const status = ok ? 'ok' : 'fail';
          const chip = { key: nextId++, tool, status };
          if (idx >= 0) chips[idx] = chip; else chips.push(chip);
          return { ...a, chips, pullProgress: tool === 'pull_hub_image' ? null : a.pullProgress };
        });
      },
      onDone: (reply) => {
        patchLastAssistant((a) => ({ ...a, content: reply || a.content,
          chips: settleRunningChips(a.chips), streaming: false }));
      },
      onError: (err) => {
        setError(apiErrorText(err, '请求失败，请稍后重试'));
        patchLastAssistant((a) => ({ ...a, chips: settleRunningChips(a.chips), streaming: false }));
      },
      onClose: () => {
        setSending(false);
        sendingRef.current = false;
        setCancelling(false);
        patchLastAssistant((a) => ({ ...a, chips: settleRunningChips(a.chips), streaming: false }));
      },
    });
  };

  // 停止：只向后端发 cancel，绝不 abort fetch/SSE —— 后端在中断点返回
  // done("请求中断")，继续读流才能收到并展示这条收尾回复（见 agent_loop 的
  // cancel_check / INTERRUPT_REPLY）。若请求恰在自然结束瞬间被点停止，
  // cancel 幂等返回 200，无副作用；onDone/onClose 照常落定气泡。
  const handleStop = async () => {
    if ((!sending && !leftover) || cancelling) return;
    setCancelling(true);
    setError('');
    try {
      await api.post('/api/agent/cancel');
      // 不在此改 sending：等后端 done("请求中断") → onClose 统一收尾。
    } catch (err) {
      setError(apiErrorText(err, '停止请求发送失败，可重试'));
      setCancelling(false); // 允许再次点击停止
    }
  };

  const handleClear = async () => {
    if (!window.confirm('清空当前会话历史？')) return;
    try {
      await api.delete('/api/agent/session');
      setMessages([]);
    } catch (err) {
      setError(apiErrorText(err, '清空失败'));
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
              <h2 style={{ margin: 0 }}>智能助手</h2>
              <button className="btn btn-secondary" onClick={handleClear}
                disabled={sending || leftover || messages.length === 0}>
                清空会话
              </button>
            </div>
            <p style={{ fontSize: 12, color: '#aaa', marginTop: 4 }}>
              我可以查询、创建、启动、停止、删除和重建你的容器，也能设置清理保护。管理员还可以搜索和拉取 Docker Hub 镜像。删除容器会销毁容器本体，工作区仍会保留。
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
                    {m.pullProgress !== undefined && m.pullProgress !== null && (
                      <div className="pull-progress" role="progressbar" aria-label="镜像拉取进度"
                        aria-valuenow={m.pullProgress} aria-valuemin="0" aria-valuemax="100">
                        <div className="pull-progress-bar" style={{ width: `${m.pullProgress}%` }} />
                        <span>镜像拉取 {m.pullProgress}%</span>
                      </div>
                    )}
                    {m.role === 'assistant' && m.chips.length > 0 && (
                      <div className="chat-chips">
                        {m.chips.map((c) => <ToolChip key={c.key} c={c} />)}
                      </div>
                    )}
                  </div>
                </div>
              ))}
              {/* 导航回来时显示后台任务，完成后以落库回复替换。 */}
              {leftover && !sending && (
                <div className="chat-msg chat-assistant">
                  <div className="chat-bubble">
                    <div style={{ whiteSpace: 'pre-wrap' }}>
                      {cancelling ? '正在停止后台请求…' : '上一条请求正在后台执行…'}
                    </div>
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
                <button className="btn btn-danger" onClick={handleStop} disabled={cancelling}>
                  {cancelling ? '正在停止…' : '停止'}
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
