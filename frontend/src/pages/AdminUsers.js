import React, { useState, useEffect } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const AdminUsers = () => {
  const [users, setUsers] = useState([]);
  const [user, setUser] = useState(null);
  const [containers, setContainers] = useState([]);
  const [expandedUser, setExpandedUser] = useState(null);

  useEffect(() => {
    fetchUsers();
    fetchContainers();
    const userStr = localStorage.getItem('user');
    if (userStr) setUser(JSON.parse(userStr));
  }, []);

  const fetchUsers = async () => {
    try {
      const res = await api.get('/api/admin/users');
      setUsers(res.data);
    } catch (err) {
      console.error(err);
    }
  };

  const fetchContainers = async () => {
    try {
      const res = await api.get('/api/containers');
      setContainers(res.data);
    } catch (err) {
      console.error(err);
    }
  };

  const handleQuotaChange = async (userId, newQuota) => {
    try {
      await api.put(`/api/admin/users/${userId}/quota`, { gpu_quota: parseInt(newQuota) });
      alert('Quota updated');
      fetchUsers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error updating quota');
    }
  };

  const handleDelete = async (userId, username) => {
    if (!window.confirm(`Delete user "${username}"? This will release all their GPU allocations and remove their container records.`)) {
      return;
    }
    try {
      await api.delete(`/api/admin/users/${userId}`);
      alert('User deleted');
      fetchUsers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error deleting user');
    }
  };

  const handleContainerStop = async (instanceId) => {
    try {
      await api.delete(`/api/containers/${instanceId}`);
      alert('Container stopped');
      fetchContainers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error stopping container');
    }
  };

  const handleContainerDelete = async (instanceId) => {
    if (!window.confirm('Delete this container? This cannot be undone.')) return;
    try {
      await api.delete(`/api/containers/${instanceId}/remove`);
      alert('Container deleted');
      fetchContainers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error deleting container');
    }
  };

  const toggleExpand = (userId) => {
    setExpandedUser(expandedUser === userId ? null : userId);
  };

  const getContainersForUser = (userId) => {
    return containers.filter(c => c.user_id === userId);
  };

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <h2>User Management</h2>
          {users.length === 0 ? (
            <p style={{ marginTop: 16, color: '#888' }}>No users found.</p>
          ) : (
            <table style={{ marginTop: 16, width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={{ width: 40 }}></th>
                  <th>ID</th>
                  <th>Username</th>
                  <th>GPU Quota</th>
                  <th>GPU Used</th>
                  <th>Containers</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u, idx) => {
                  const userContainers = getContainersForUser(u.id);
                  const isExpanded = expandedUser === u.id;
                  return (
                    <React.Fragment key={u.id}>
                      <tr style={{ background: isExpanded ? '#f5f5f5' : 'transparent' }}>
                        <td>
                          <span onClick={() => toggleExpand(u.id)}
                            style={{ cursor: 'pointer', userSelect: 'none', fontSize: 16 }}>
                            {isExpanded ? '▼' : '▶'}
                          </span>
                        </td>
                        <td>{idx + 1}</td>
                        <td>{u.username}</td>
                        <td>
                          <input type="number" min="1" max="4"
                            defaultValue={u.gpu_quota}
                            style={{ width: 80, padding: '4px 8px' }}
                            id={`quota-${u.id}`} />
                        </td>
                        <td>{u.gpu_used}</td>
                        <td>{userContainers.length}</td>
                        <td>
                          <button className="btn btn-primary"
                            onClick={() => {
                              const input = document.getElementById(`quota-${u.id}`);
                              handleQuotaChange(u.id, input.value);
                            }}>
                            Set
                          </button>
                          <button className="btn btn-danger"
                            style={{ marginLeft: 8 }}
                            onClick={() => handleDelete(u.id, u.username)}>
                            Delete
                          </button>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr>
                          <td colSpan={7} style={{ padding: 0 }}>
                            <div style={{ padding: '8px 16px 16px 40px', background: '#fafafa' }}>
                              {userContainers.length === 0 ? (
                                <p style={{ color: '#888', margin: 8 }}>No containers for this user.</p>
                              ) : (
                                <table style={{ width: '100%', fontSize: 13 }}>
                                  <thead>
                                    <tr>
                                      <th>Container ID</th>
                                      <th>Image</th>
                                      <th>Port</th>
                                      <th>GPUs</th>
                                      <th>Password</th>
                                      <th>Status</th>
                                      <th>Action</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {userContainers.map(c => (
                                      <tr key={c.id}>
                                        <td style={{ fontFamily: 'monospace' }}>{c.container_id}</td>
                                        <td>{c.image}</td>
                                        <td>{c.assigned_port || '-'}</td>
                                        <td>{c.gpu_count} (IDs: {c.gpu_ids?.join(',')})</td>
                                        <td>
                                          {c.access_password ? (
                                            <span style={{ cursor: 'pointer', fontFamily: 'monospace', fontSize: 13 }}
                                              onClick={() => {
                                                const el = document.createElement('textarea');
                                                el.value = c.access_password;
                                                el.style.position = 'fixed';
                                                el.style.opacity = '0';
                                                document.body.appendChild(el);
                                                el.select();
                                                document.execCommand('copy');
                                                document.body.removeChild(el);
                                                alert('Password copied!');
                                              }}
                                              title="Click to copy password">
                                              {'●'.repeat(8)}
                                            </span>
                                          ) : '-'}
                                        </td>
                                        <td>
                                          <span className="status-badge"
                                            style={{ background: c.status === 'running' ? '#52c41a' : '#888' }}>
                                            {c.status}
                                          </span>
                                        </td>
                                        <td>
                                          {c.status === 'running' && (
                                            <button className="btn btn-danger"
                                              onClick={() => handleContainerStop(c.id)}
                                              style={{ marginRight: 4, fontSize: 12, padding: '2px 8px' }}>
                                              Stop
                                            </button>
                                          )}
                                          <button className="btn btn-secondary"
                                            onClick={() => handleContainerDelete(c.id)}
                                            style={{ fontSize: 12, padding: '2px 8px' }}>
                                            Delete
                                          </button>
                                        </td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              )}
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

export default AdminUsers;
