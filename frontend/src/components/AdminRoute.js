import React from 'react';
import { Navigate } from 'react-router-dom';

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
      <h2>403 Forbidden</h2>
      <p>Admin access required.</p>
      <a href="/dashboard">Back to Dashboard</a>
    </div>
  );
};

export default AdminRoute;
