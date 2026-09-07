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
      setError(err.response?.data?.detail || 'Operation failed');
    }
  };

  return (
    <div className="login-container">
      <form onSubmit={handleSubmit} className="login-form">
        <h2>{isRegister ? 'Register' : 'Login'}</h2>
        {error && <div className="error">{error}</div>}
        <div className="form-group">
          <label>Username:</label>
          <input type="text" value={username}
            onChange={(e) => setUsername(e.target.value)} required />
        </div>
        <div className="form-group">
          <label>Password:</label>
          <input type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <button type="submit" className="btn btn-primary" style={{ width: '100%' }}>
          {isRegister ? 'Register' : 'Login'}
        </button>
        <div className="register-link">
          {isRegister ? (
            <>Already have an account? <Link to="#" onClick={() => { setIsRegister(false); setError(''); }}>Login</Link></>
          ) : (
            <>Don't have an account? <Link to="#" onClick={() => { setIsRegister(true); setError(''); }}>Register</Link></>
          )}
        </div>
      </form>
    </div>
  );
};

export default Login;
