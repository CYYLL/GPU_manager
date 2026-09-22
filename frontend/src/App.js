import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Login from './pages/Login';
import Home from './pages/Home';
import Guide from './pages/Guide';
import Dashboard from './pages/Dashboard';
import ContainerManagement from './pages/ContainerManagement';
import UserProfile from './pages/UserProfile';
import AdminUsers from './pages/AdminUsers';
import AdminImages from './pages/AdminImages';
import AgentChat from './pages/AgentChat';
import AdminCleanupLog from './pages/AdminCleanupLog';
import ProtectedRoute from './components/ProtectedRoute';
import AdminRoute from './components/AdminRoute';
import UserRoute from './components/UserRoute';
import { ModeProvider } from './context/ModeContext';
import './App.css';

function App() {
  return (
    <Router>
      <ModeProvider>
      <div className="app-container">
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/" element={
            <ProtectedRoute><Home /></ProtectedRoute>
          } />
          <Route path="/guide" element={
            <ProtectedRoute><Guide /></ProtectedRoute>
          } />
          <Route path="/dashboard" element={
            <ProtectedRoute><Dashboard /></ProtectedRoute>
          } />
          <Route path="/containers" element={
            <ProtectedRoute><UserRoute><ContainerManagement /></UserRoute></ProtectedRoute>
          } />
          <Route path="/chat" element={
            <ProtectedRoute><AgentChat /></ProtectedRoute>
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
          <Route path="/admin/cleanup" element={
            <ProtectedRoute><AdminRoute><AdminCleanupLog /></AdminRoute></ProtectedRoute>
          } />
        </Routes>
      </div>
      </ModeProvider>
    </Router>
  );
}

export default App;
