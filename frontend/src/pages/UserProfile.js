import { apiErrorText } from '../services/errorMessages';
import React, { useState, useEffect } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const UserProfile = () => {
  // 先以 localStorage 里的账号信息渲染（含导航栏），再向服务端刷新用量等实时字段。
  // 避免 /api/users/me 一次瞬时失败（如 sqlite busy 的 500）就让页面永久停在 loading。
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('user') || 'null'); } catch (e) { return null; }
  });
  const [fetchError, setFetchError] = useState('');
  const [pwForm, setPwForm] = useState({ old_password: '', new_password: '', confirm: '' });
  const [pwMsg, setPwMsg] = useState('');
  const [resetForm, setResetForm] = useState({ username: '' });
  const [resetMsg, setResetMsg] = useState('');
  const [checkedUser, setCheckedUser] = useState(null);

  const loadProfile = () => {
    setFetchError('');
    api.get('/api/users/me')
      .then((res) => {
        setUser(res.data);
        try { localStorage.setItem('user', JSON.stringify(res.data)); } catch (e) { /* ignore */ }
      })
      .catch(() => setFetchError('无法加载个人资料（服务器暂时不可用）。已显示本地缓存的资料。'));
  };

  useEffect(() => {
    loadProfile();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleChangePassword = async (e) => {
    e.preventDefault();
    setPwMsg('');
    if (pwForm.new_password !== pwForm.confirm) {
      setPwMsg('两次输入的密码不一致');
      return;
    }
    if (pwForm.new_password.length < 6) {
      setPwMsg('新密码至少需要 6 个字符');
      return;
    }
    try {
      await api.put('/api/users/password', {
        old_password: pwForm.old_password,
        new_password: pwForm.new_password,
      });
      setPwMsg('密码修改成功');
      setPwForm({ old_password: '', new_password: '', confirm: '' });
    } catch (err) {
      setPwMsg(apiErrorText(err, '修改密码失败'));
    }
  };

  const handleAdminReset = async (e) => {
    e.preventDefault();
    setResetMsg('');
    setCheckedUser(null);
    // First check if user exists
    try {
      const checkRes = await api.get(`/api/admin/users/check/${resetForm.username}`);
      setCheckedUser(checkRes.data);
      setResetMsg(`已找到用户“${checkRes.data.username}”，请确认是否将密码重置为 0000000`);
    } catch (err) {
      setResetMsg(apiErrorText(err, '未找到该用户'));
    }
  };

  const handleConfirmReset = async () => {
    setResetMsg('');
    try {
      await api.put('/api/admin/users/reset-password', {
        username: resetForm.username,
        new_password: '0000000',
      });
      setResetMsg(`用户“${resetForm.username}”的密码已重置为 0000000`);
      setCheckedUser(null);
      setResetForm({ username: '' });
    } catch (err) {
      setResetMsg(apiErrorText(err, '重置密码失败'));
    }
  };

  if (!user) {
    // 无本地缓存且请求失败：给错误页 + 重试，而不是无限 loading。
    if (fetchError) {
      return (
        <div>
          <div className="content">
            <div className="card">
              <h2>个人资料</h2>
              <p style={{ color: '#cf1322' }}>{fetchError}</p>
              <button className="btn btn-primary" onClick={loadProfile}>重试</button>
            </div>
          </div>
        </div>
      );
    }
    return <div>正在加载…</div>;
  }

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        {fetchError && (
          <div className="card" style={{ padding: '12px 16px', background: '#fff7e6', border: '1px solid #ffd591' }}>
            <span style={{ color: '#d46b08', fontSize: 13 }}>{fetchError}</span>{' '}
            <button className="btn" style={{ padding: '2px 8px', fontSize: 12 }} onClick={loadProfile}>重试</button>
          </div>
        )}
        <div className="card">
          <h2>个人资料</h2>
          <table style={{ marginTop: 16 }}>
            <tbody>
              <tr><td style={{ fontWeight: 600, width: 150 }}>用户名</td><td>{user.username}</td></tr>
              <tr><td style={{ fontWeight: 600 }}>角色</td><td>{user.role === 'admin' ? '管理员' : '普通用户'}</td></tr>
              {user.role !== 'admin' && (
                <>
                  <tr><td style={{ fontWeight: 600 }}>GPU 配额</td><td>{user.gpu_quota}</td></tr>
                  <tr><td style={{ fontWeight: 600 }}>已使用 GPU</td><td>{user.gpu_used}</td></tr>
                  <tr><td style={{ fontWeight: 600 }}>剩余 GPU 配额</td><td>{(user.gpu_quota || 0) - (user.gpu_used || 0)}</td></tr>
                </>
              )}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>修改密码</h2>
          <form onSubmit={handleChangePassword} style={{ marginTop: 16 }}>
            <div style={{ marginBottom: 12 }}>
              <input type="password" placeholder="当前密码" required
                value={pwForm.old_password}
                onChange={e => setPwForm({ ...pwForm, old_password: e.target.value })}
                style={{ width: 250 }} />
            </div>
            <div style={{ marginBottom: 12 }}>
              <input type="password" placeholder="新密码（至少 6 个字符）" required
                value={pwForm.new_password}
                onChange={e => setPwForm({ ...pwForm, new_password: e.target.value })}
                style={{ width: 250 }} />
            </div>
            <div style={{ marginBottom: 12 }}>
              <input type="password" placeholder="确认新密码" required
                value={pwForm.confirm}
                onChange={e => setPwForm({ ...pwForm, confirm: e.target.value })}
                style={{ width: 250 }} />
            </div>
            <button type="submit" className="btn">修改密码</button>
            {pwMsg && <p style={{ color: pwMsg.includes('成功') ? '#52c41a' : '#ff4d4f', marginTop: 8 }}>{pwMsg}</p>}
          </form>
        </div>

        {user.role === 'admin' && (
          <div className="card">
            <h2>用户管理与密码重置</h2>
            <p style={{ fontSize: 13, color: '#888', marginBottom: 16 }}>
              先通过用户名确认账号，再将其密码重置为默认密码 <strong>0000000</strong>。
            </p>
            <form onSubmit={handleAdminReset} style={{ marginTop: 16 }}>
              <div style={{ marginBottom: 12 }}>
                <input type="text" placeholder="用户名" required
                  value={resetForm.username}
                  onChange={e => {
                    setResetForm({ ...resetForm, username: e.target.value });
                    setCheckedUser(null);
                    setResetMsg('');
                  }}
                  style={{ width: 250 }} />
              </div>
              {!checkedUser ? (
                <button type="submit" className="btn btn-primary">查找用户</button>
              ) : (
                <div>
                  <div style={{ marginBottom: 12, padding: 12, background: '#f0f5ff', borderRadius: 4, fontSize: 14 }}>
                    <strong>{checkedUser.username}</strong>（{checkedUser.role === 'admin' ? '管理员' : '普通用户'}）
                    <br />编号：{checkedUser.id}｜GPU 配额：{checkedUser.gpu_quota}
                  </div>
                  <p style={{ fontSize: 13, color: '#888', marginBottom: 12 }}>
                    密码将重置为 <strong>0000000</strong>
                  </p>
                  <button type="button" className="btn btn-primary" onClick={handleConfirmReset}
                    style={{ marginRight: 8 }}>
                    确认重置
                  </button>
                  <button type="button" className="btn btn-secondary"
                    onClick={() => { setCheckedUser(null); setResetMsg(''); }}>
                    取消
                  </button>
                </div>
              )}
              {resetMsg && <p style={{ color: /已找到|已重置/.test(resetMsg) ? '#52c41a' : '#ff4d4f', marginTop: 8 }}>{resetMsg}</p>}
            </form>
          </div>
        )}
      </div>
    </div>
  );
};

export default UserProfile;
