import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import api from '../services/api';

// 双模式单数据源：后端 User.mode（GET/PUT /api/mode）。登录时 /api/users/me 返回
// mode 缓存进 localStorage 'user'；此处以它初始化，再向服务器刷新作权威。
const ModeContext = createContext({ mode: 'llm', setMode: async () => {}, refresh: () => {}, loading: true });

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

export function ModeProvider({ children }) {
  const [mode, setModeState] = useState(readLocalMode);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!localStorage.getItem('token')) { setLoading(false); return; }
    try {
      const res = await api.get('/api/mode');
      const m = res.data?.mode === 'traditional' ? 'traditional' : 'llm';
      setModeState(m);
      writeLocalMode(m);
    } catch (e) {
      // 401 handled globally (redirect); other errors → keep cached mode.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const setMode = useCallback(async (m) => {
    const next = m === 'traditional' ? 'traditional' : 'llm';
    setModeState(next);
    writeLocalMode(next);        // 乐观更新
    try {
      await api.put('/api/mode', { mode: next });
    } catch (e) {
      // 后端未接受则保持乐观值（/api/mode 只接受两个字面量，理论不会发生）
    }
  }, []);

  return (
    <ModeContext.Provider value={{ mode, setMode, refresh, loading }}>
      {children}
    </ModeContext.Provider>
  );
}
