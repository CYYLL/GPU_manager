import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Login from './pages/Login';
import Home from './pages/Home';
import Dashboard from './pages/Dashboard';
import ContainerManagement from './pages/ContainerManagement';
import UserProfile from './pages/UserProfile';
import AdminUsers from './pages/AdminUsers';
import AdminImages from './pages/AdminImages';
import ProtectedRoute from './components/ProtectedRoute';
import AdminRoute from './components/AdminRoute';
import UserRoute from './components/UserRoute';
import './App.css';

function App() {
  return (
    <Router>
      <div className="app-container">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={
            <ProtectedRoute><Home /></ProtectedRoute>
          } />
          <Route path="/dashboard" element={
            <ProtectedRoute><Dashboard /></ProtectedRoute>
          } />
          <Route path="/containers" element={
            <ProtectedRoute><UserRoute><ContainerManagement /></UserRoute></ProtectedRoute>
          } />
          <Route path="/profile" element={
            <ProtectedRoute><UserProfile /></ProtectedRoute>
          } />
          <Route path="/admin/users" element={
            <ProtectedRoute><AdminRoute><AdminUsers /></AdminRoute></ProtectedRoute>
          } />
          <Route path="/admin/images" element={
            <ProtectedRoute><AdminRoute><AdminImages /></AdminRoute></ProtectedRoute>
          } />
        </Routes>
      </div>
    </Router>
  );
}

export default App;
