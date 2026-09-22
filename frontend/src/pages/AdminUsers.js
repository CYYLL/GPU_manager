import { apiErrorText } from '../services/errorMessages';
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
      alert('GPU 配额已更新');
      fetchUsers();
    } catch (err) {
      alert(apiErrorText(err, '更新 GPU 配额失败'));
    }
  };

  const handleDelete = async (userId, username) => {
    if (!window.confirm(`确定删除用户“${username}”吗？这会释放其 GPU 分配并删除容器记录。`)) {
      return;
    }
    try {
      await api.delete(`/api/admin/users/${userId}`);
      alert('用户已删除');
      fetchUsers();
    } catch (err) {
      alert(apiErrorText(err, '删除用户失败'));
    }
  };

  const handleContainerStop = async (instanceId) => {
    try {
      await api.delete(`/api/containers/${instanceId}`);
      alert('容器已停止');
      fetchContainers();
    } catch (err) {
      alert(apiErrorText(err, '停止容器失败'));
    }
  };

  const handleContainerDelete = async (instanceId) => {
    if (!window.confirm('确定删除该容器吗？此操作无法撤销。')) return;
    try {
      await api.delete(`/api/containers/${instanceId}/remove`);
      alert('容器已删除');
      fetchContainers();
    } catch (err) {
      alert(apiErrorText(err, '删除容器失败'));
    }
  };

  const toggleExpand = (userId) => {
    setExpandedUser(expandedUser === userId ? null : userId);
  };

  const getContainersForUser = (userId) => {
    return containers.filter(c => c.user_id === userId);
  };
  const statusLabel = {
    running: '运行中', stopped: '已停止', removed: '已移除', error: '异常',
  };

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <h2>用户管理</h2>
          {users.length === 0 ? (
            <p style={{ marginTop: 16, color: '#888' }}>暂无用户。</p>
          ) : (
            <table style={{ marginTop: 16, width: '100%', borderCollapse: 'collapse' }}>
              <thead>
                <tr>
                  <th style={{ width: 40 }}></th>
                  <th>编号</th>
                  <th>用户名</th>
                  <th>GPU 配额</th>
                  <th>已用 GPU</th>
                  <th>容器数</th>
                  <th>操作</th>
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
                            保存配额
                          </button>
                          <button className="btn btn-danger"
                            style={{ marginLeft: 8 }}
                            onClick={() => handleDelete(u.id, u.username)}>
                            删除
                          </button>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr>
                          <td colSpan={7} style={{ padding: 0 }}>
                            <div style={{ padding: '8px 16px 16px 40px', background: '#fafafa' }}>
                              {userContainers.length === 0 ? (
                                <p style={{ color: '#888', margin: 8 }}>该用户暂无容器。</p>
                              ) : (
                                <table style={{ width: '100%', fontSize: 13 }}>
                                  <thead>
                                    <tr>
                                      <th>容器编号</th>
                                      <th>镜像</th>
                                      <th>端口</th>
                                      <th>SSH 用户</th>
                                      <th>GPU</th>
                                      <th>密码</th>
                                      <th>状态</th>
                                      <th>操作</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {userContainers.map(c => (
                                      <tr key={c.id}>
                                        <td style={{ fontFamily: 'monospace' }}>{c.container_id}</td>
                                        <td>{c.image}</td>
                                        <td>{c.assigned_port || '-'}</td>
                                        <td>{c.ssh_username || '未确认'}</td>
                                        <td>{c.gpu_count} 张（编号：{c.gpu_ids?.join(',')}）</td>
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
                                                alert('密码已复制');
                                              }}
                                              title="点击复制密码">
                                              {'●'.repeat(8)}
                                            </span>
                                          ) : '-'}
                                        </td>
                                        <td>
                                          <span className="status-badge"
                                            style={{ background: c.status === 'running' ? '#52c41a' : '#888' }}>
                                            {statusLabel[c.status] || '未知'}
                                          </span>
                                        </td>
                                        <td>
                                          {c.status === 'running' && (
                                            <button className="btn btn-danger"
                                              onClick={() => handleContainerStop(c.id)}
                                              style={{ marginRight: 4, fontSize: 12, padding: '2px 8px' }}>
                                              停止
                                            </button>
                                          )}
                                          <button className="btn btn-secondary"
                                            onClick={() => handleContainerDelete(c.id)}
                                            style={{ fontSize: 12, padding: '2px 8px' }}>
                                            删除
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
