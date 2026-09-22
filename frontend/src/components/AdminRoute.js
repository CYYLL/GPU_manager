import React from 'react';
import { Navigate, Link } from 'react-router-dom';

const AdminRoute = ({ children }) => {
  const token = localStorage.getItem('token');
  const userStr = localStorage.getItem('user');

  if (!token) {
    return <Navigate to="/login" replace />;
  }

  if (userStr) {
    try {
      const user = JSON.parse(userStr);
      if (user.role === 'admin') {
        return children;
      }
    } catch {
      // fall through to 403
    }
  }

  return (
    <div className="error-page">
      <h2>无权访问</h2>
      <p>此页面仅限管理员使用。</p>
      <Link to="/dashboard">返回 GPU 状态页</Link>
    </div>
  );
};

export default AdminRoute;
