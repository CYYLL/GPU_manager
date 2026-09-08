import React, { createContext, useContext, useState, useEffect, useRef, useCallback } from 'react';
import api from '../services/api';

// 双模式单数据源：后端 User.mode（GET/PUT /api/mode）为权威，本文件只做同步。
// 登录时 /api/users/me 返回 mode 缓存进 localStorage 'user'；以它初始化再向服务器刷新。
// 跨标签页同步：同一浏览器其它标签页的 localStorage 变更会触发 'storage' 事件，
// 本 Provider 据此直接采用（该值来自另一标签页，而它自己会保持与服务端一致），
// 无需实时请求、也不会与服务端竞态。
const ModeContext = createContext({
  mode: 'llm', confirmed: false, setMode: async () => {},
  refresh: () => {}, loading: true, modeError: null,
});

export const useMode = () => useContext(ModeContext);

const readLocalMode = () => {
  try {
    const u = JSON.parse(localStorage.getItem('user') || 'null');
    if (u && (u.mode === 'llm' || u.mode === 'traditional')) return u.mode;
  } catch (e) { /* ignore */ }
  const m = localStorage.getItem('mode');
  return m === 'traditional' ? 'traditional' : 'llm';
};

const writeLocalMode = (m) => {
  localStorage.setItem('mode', m);
  try {
    const u = JSON.parse(localStorage.getItem('user') || 'null');
    if (u) { u.mode = m; localStorage.setItem('user', JSON.stringify(u)); }
  } catch (e) { /* ignore */ }
};

const norm = (m) => (m === 'traditional' ? 'traditional' : 'llm');

// 切换写库失败时展示（模式仍乐观地先改、失败回滚，绝不静默假装成功）。
const SAVE_FAIL_MSG = '⚠ 切换未保存：无法写入服务器，已还原为原模式';

export function ModeProvider({ children }) {
  const [mode, setModeState] = useState(readLocalMode);
  const [loading, setLoading] = useState(true);
  const [confirmed, setConfirmed] = useState(false);  // 是否已由服务端确认
  const [modeError, setModeError] = useState(null);
  const errTimer = useRef(null);
  // 同步镜像当前 mode，供回滚目标与重复点击判定使用（state 更新是异步的）。
  const modeRef = useRef(readLocalMode());

  const applyMode = useCallback((m) => {
    modeRef.current = m;
    setModeState(m);
  }, []);

  const showError = useCallback((msg) => {
    if (errTimer.current) clearTimeout(errTimer.current);
    setModeError(msg);
    errTimer.current = setTimeout(() => setModeError(null), 5000);
  }, []);

  const clearError = useCallback(() => {
    if (errTimer.current) clearTimeout(errTimer.current);
    errTimer.current = null;
    setModeError(null);
  }, []);

  const refresh = useCallback(async () => {
    if (!localStorage.getItem('token')) { setLoading(false); return; }
    try {
      const res = await api.get('/api/mode');
      const m = norm(res.data?.mode);
      applyMode(m);
      writeLocalMode(m);
      setConfirmed(true);   // 服务端确认过 → LLM-only 界面(如 Agent)可安全按 mode 显示
    } catch (e) {
      // 401 handled globally (redirect); other errors → keep cached mode.
    } finally {
      setLoading(false);
    }
  }, [applyMode]);

  useEffect(() => { refresh(); }, [refresh]);

  // 跨标签页同步：其它标签页写入 localStorage 时采用其值（保持本页 UI 与账号状态一致）。
  useEffect(() => {
    const onStorage = (e) => {
      if (!localStorage.getItem('token')) return;      // 未登录不响应
      if (e.key !== 'mode' && e.key !== 'user') return;
      if (e.newValue == null) return;                  // 键被移除(如登出)，不采信
      const m = norm(readLocalMode());
      if (m !== modeRef.current) applyMode(m);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [applyMode]);

  const setMode = useCallback(async (m) => {
    const next = norm(m);
    if (next === modeRef.current) { clearError(); return true; }  // no-op
    const prev = modeRef.current;
    clearError();
    applyMode(next);
    writeLocalMode(next);        // 乐观更新；并让其它标签页经 storage 事件跟随
    try {
      await api.put('/api/mode', { mode: next });
      setConfirmed(true);   // 切换被服务端接受 → 这就是权威值
      return true;
    } catch (e) {
      // 后端未接受：回滚。仅在用户没有继续切到更新的模式时才回滚，
      // 避免把用户刚做出的下一个选择覆盖掉。
      if (modeRef.current === next) {
        applyMode(prev);
        writeLocalMode(prev);
      }
      showError(SAVE_FAIL_MSG);
      return false;
    }
  }, [applyMode, clearError, showError]);

  useEffect(() => () => { if (errTimer.current) clearTimeout(errTimer.current); }, []);

  return (
    <ModeContext.Provider value={{ mode, confirmed, setMode, refresh, loading, modeError }}>
      {children}
    </ModeContext.Provider>
  );
}
