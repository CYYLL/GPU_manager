import { apiErrorText } from '../services/errorMessages';
import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import api from '../services/api';
import { useMode } from '../context/ModeContext';

const Login = () => {
  const [isRegister, setIsRegister] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const navigate = useNavigate();
  const { refresh: refreshMode } = useMode();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');

    try {
      if (isRegister) {
        await api.post('/api/users/register', { username, password });
        // After register, log in automatically
        const loginRes = await api.post('/api/users/login', { username, password });
        localStorage.setItem('token', loginRes.data.access_token);
        localStorage.setItem('tokenType', loginRes.data.token_type);
        const userRes = await api.get('/api/users/me');
        localStorage.setItem('user', JSON.stringify(userRes.data));
        await refreshMode();   // 同步 ModeContext 到新会话的模式（服务端权威）
        navigate('/');
      } else {
        const response = await api.post('/api/users/login', { username, password });
        localStorage.setItem('token', response.data.access_token);
        localStorage.setItem('tokenType', response.data.token_type);
        const userRes = await api.get('/api/users/me');
        localStorage.setItem('user', JSON.stringify(userRes.data));
        await refreshMode();
        navigate('/');
      }
    } catch (err) {
      setError(apiErrorText(err, '操作失败'));
    }
  };

  return (
    <div className="login-container">
      <form onSubmit={handleSubmit} className="login-form">
        <h2>{isRegister ? '注册账号' : '登录'}</h2>
        {error && <div className="error">{error}</div>}
        <div className="form-group">
          <label>用户名：</label>
          <input type="text" value={username}
            onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="form-group">
          <label>密码：</label>
          <input type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <button type="submit" className="btn btn-primary" style={{ width: '100%' }}>
          {isRegister ? '注册' : '登录'}
        </button>
        <div className="register-link">
          {isRegister ? (
            <>已有账号？<Link to="#" onClick={() => { setIsRegister(false); setError(''); }}>去登录</Link></>
          ) : (
            <>还没有账号？<Link to="#" onClick={() => { setIsRegister(true); setError(''); }}>去注册</Link></>
          )}
        </div>
      </form>
    </div>
  );
};

export default Login;
