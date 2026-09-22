import React from 'react';
import { useNavigate, useLocation, Link, NavLink } from 'react-router-dom';
import ModeToggle from './ModeToggle';
import { useMode } from '../context/ModeContext';

const cachedUser = () => {
  try {
    return JSON.parse(localStorage.getItem('user') || 'null');
  } catch {
    return null;
  }
};

const AppHeader = ({ user }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { mode, confirmed } = useMode();
  // Route pages load their user state in an effect. Use the same cached account
  // that AdminRoute checks on the first render, so admin links do not pop in later.
  const visibleUser = user || cachedUser();

  const handleLogout = () => {
    localStorage.removeItem('token');
    localStorage.removeItem('tokenType');
    localStorage.removeItem('user');
    navigate('/login');
  };

  const navClass = ({ isActive }) => `app-nav-link${isActive ? ' active' : ''}`;

  return (
    <header className="app-header">
      <div className="app-header-main">
        {/* 内部路由一律用 <Link>（client-side）。原生 <a href> 会整页刷新 → ModeProvider
            重新挂载、confirmed 归 false，Agent 导航项要等 /api/mode 确认后才重新出现，
            表现为"切页时 Agent 栏消失又冒出"。 */}
        <h1><Link to="/" style={{ color: '#fff', textDecoration: 'none' }}>GPU 资源管理平台</Link></h1>
        <nav className="app-nav" aria-label="主导航">
          <NavLink to="/" end className={navClass}>首页</NavLink>
          <NavLink to="/guide" className={navClass}>使用指南</NavLink>
          <NavLink to="/dashboard" className={navClass}>GPU 状态</NavLink>
          {/* Agent 仅对服务端已确认的 LLM 账号显示；传统/未确认时不出现，
              避免用 localStorage 旧值把 Agent 闪给传统账号（含服务端暂不可达时）。 */}
          {confirmed && mode === 'llm' && (
            <NavLink to="/chat" className={navClass}>智能助手</NavLink>
          )}
          {visibleUser?.role !== 'admin' && <NavLink to="/containers" className={navClass}>我的容器</NavLink>}
          <NavLink to="/profile" className={navClass}>个人资料</NavLink>
          {visibleUser?.role === 'admin' && (
            <>
              <NavLink to="/admin/users" className={navClass}>用户管理</NavLink>
              <NavLink to="/admin/images" className={navClass}>镜像管理</NavLink>
              <NavLink to="/admin/cleanup" className={navClass}>清理与告警</NavLink>
            </>
          )}
        </nav>
      </div>
      <div className="user-info">
        <span>{visibleUser?.username}（{visibleUser?.role === 'admin' ? '管理员' : '用户'}）</span>
        {/* 模式开关仅在 GPU Resource Manager 主页(/)允许切换，其它页面不显示 */}
        {location.pathname === '/' && <ModeToggle compact />}
        <button className="logout-btn" onClick={handleLogout}>退出登录</button>
      </div>
    </header>
  );
};

export default AppHeader;
