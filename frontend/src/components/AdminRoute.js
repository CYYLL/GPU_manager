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
      <h2>403 Forbidden</h2>
      <p>Admin access required.</p>
      <Link to="/dashboard">Back to Dashboard</Link>
    </div>
  );
};

export default AdminRoute;
