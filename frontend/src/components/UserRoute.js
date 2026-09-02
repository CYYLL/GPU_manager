import React from 'react';
import { Navigate } from 'react-router-dom';

const UserRoute = ({ children }) => {
  const userStr = localStorage.getItem('user');
  if (userStr) {
    try {
      const user = JSON.parse(userStr);
      if (user.role === 'admin') {
        return <Navigate to="/dashboard" replace />;
      }
    } catch (e) {
      // ignore parse error
    }
  }
  return children;
};

export default UserRoute;
