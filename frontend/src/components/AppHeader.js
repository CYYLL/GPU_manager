import React from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import ModeToggle from './ModeToggle';

const AppHeader = ({ user }) => {
  const navigate = useNavigate();
  const location = useLocation();

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
        <h1><a href="/" style={{ color: '#fff', textDecoration: 'none' }}>GPU Resource Manager</a></h1>
        <nav>
          <a href="/dashboard">Dashboard</a>
          {user?.role !== 'admin' && <a href="/containers">Containers</a>}
          <a href="/profile">Profile</a>
          {user?.role === 'admin' && (
            <>
              <a href="/admin/users" style={{ fontWeight: isAdminPage ? 'bold' : 'normal' }}>
                Admin
              </a>
              {isAdminPage && (
                <span style={{ fontSize: 13, color: '#aaa' }}>
                  [ <a href="/admin/users">Users</a> | <a href="/admin/images">Images</a> ]
                </span>
              )}
            </>
          )}
        </nav>
      </div>
      <div className="user-info">
        <span>{user?.username} ({user?.role})</span>
        <ModeToggle compact />
        <button className="logout-btn" onClick={handleLogout}>Logout</button>
      </div>
    </header>
  );
};

export default AppHeader;
