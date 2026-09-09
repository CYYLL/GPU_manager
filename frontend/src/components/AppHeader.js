import React from 'react';
import { useNavigate, useLocation, Link } from 'react-router-dom';
import ModeToggle from './ModeToggle';
import { useMode } from '../context/ModeContext';

const AppHeader = ({ user }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { mode, confirmed } = useMode();

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('tokenType');
    localStorage.removeItem('user');
    navigate('/login');
  };

  const isAdminPage = location.pathname.startsWith('/admin');

  return (
    <header className="app-header">
      <div style={{ display: 'flex', alignItems: 'center', gap: 32 }}>
        {/* 内部路由一律用 <Link>（client-side）。原生 <a href> 会整页刷新 → ModeProvider
            重新挂载、confirmed 归 false，Agent 导航项要等 /api/mode 确认后才重新出现，
            表现为"切页时 Agent 栏消失又冒出"。 */}
        <h1><Link to="/" style={{ color: '#fff', textDecoration: 'none' }}>GPU Resource Manager</Link></h1>
        <nav>
          <Link to="/dashboard">Dashboard</Link>
          {/* Agent 仅对服务端已确认的 LLM 账号显示；传统/未确认时不出现，
              避免用 localStorage 旧值把 Agent 闪给传统账号（含服务端暂不可达时）。 */}
          {confirmed && mode === 'llm' && (
            <Link to="/chat" style={{ fontWeight: location.pathname.startsWith('/chat') ? 'bold' : 'normal' }}>
              Agent
            </Link>
          )}
          {user?.role !== 'admin' && <Link to="/containers">Containers</Link>}
          <Link to="/profile">Profile</Link>
          {user?.role === 'admin' && (
            <>
              <Link to="/admin/users" style={{ fontWeight: location.pathname.startsWith('/admin/users') ? 'bold' : 'normal' }}>
                Admin
              </Link>
              <Link to="/admin/cleanup" style={{ fontWeight: location.pathname.startsWith('/admin/cleanup') ? 'bold' : 'normal' }}>
                Cleanup
              </Link>
              {isAdminPage && (
                <span style={{ fontSize: 13, color: '#aaa' }}>
                  [ <Link to="/admin/users">Users</Link> | <Link to="/admin/images">Images</Link> | <Link to="/admin/cleanup">Cleanup</Link> ]
                </span>
              )}
            </>
          )}
        </nav>
      </div>
      <div className="user-info">
        <span>{user?.username} ({user?.role})</span>
        {/* 模式开关仅在 GPU Resource Manager 主页(/)允许切换，其它页面不显示 */}
        {location.pathname === '/' && <ModeToggle compact />}
        <button className="logout-btn" onClick={handleLogout}>Logout</button>
      </div>
    </header>
  );
};

export default AppHeader;
