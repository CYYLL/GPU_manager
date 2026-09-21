import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const ContainerManagement = () => {
  const [containers, setContainers] = useState([]);
  const [images, setImages] = useState([]);
  const [user, setUser] = useState(null);
  const [hostIp, setHostIp] = useState(null);
  const [form, setForm] = useState({
    image_id: '',
    gpu_count: 1,
    cpu_limit: '',
    memory_limit: '',
  });
  const [loading, setLoading] = useState(false);
  const [processingIds, setProcessingIds] = useState({});  // { [instanceId]: "stopping" | "starting" | "deleting" }

  const fetchContainers = useCallback(async () => {
    try {
      const res = await api.get('/api/containers');
      setContainers(res.data);
    } catch (err) {
      console.error('Error fetching containers:', err);
    }
  }, []);

  useEffect(() => {
    fetchContainers();
    fetchImages();
    api.get('/api/host-ip').then(res => setHostIp(res.data.host_ip)).catch(() => {});
    const userStr = localStorage.getItem('user');
    if (userStr) setUser(JSON.parse(userStr));
    const interval = setInterval(fetchContainers, 5000);
    return () => clearInterval(interval);
  }, [fetchContainers]);

  const fetchImages = async () => {
    try {
      const res = await api.get('/api/images');
      setImages(res.data);
    } catch (err) {
      console.error('Error fetching images:', err);
    }
  };

  const handleStart = async (e) => {
    e.preventDefault();
    if (!form.image_id) return alert('Please select an image');
    setLoading(true);
    try {
      await api.post('/api/containers/start', {
        image_id: parseInt(form.image_id),
        gpu_count: form.gpu_count,
        cpu_limit: form.cpu_limit ? parseFloat(form.cpu_limit) : null,
        memory_limit: form.memory_limit ? parseInt(form.memory_limit) : null,
      });
      alert('Container started successfully');
      setForm({ image_id: '', gpu_count: 1, cpu_limit: '', memory_limit: '' });
      fetchContainers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error starting container');
    } finally {
      setLoading(false);
    }
  };

  // Helper: set processing state, run operation, keep visual feedback for at least 600ms
  const withProcessingId = async (instanceId, action, fn) => {
    setProcessingIds(prev => ({ ...prev, [instanceId]: action }));
    const start = Date.now();
    try {
      await fn();
    } finally {
      const elapsed = Date.now() - start;
      if (elapsed < 600) {
        await new Promise(resolve => setTimeout(resolve, 600 - elapsed));
      }
      setProcessingIds(prev => { const n = { ...prev }; delete n[instanceId]; return n; });
    }
  };

  const handleStop = async (instanceId) => {
    if (!window.confirm('Stop this container? GPU resources will be released.')) return;
    withProcessingId(instanceId, 'stopping', async () => {
      await api.delete(`/api/containers/${instanceId}`);
      alert('Container stopped');
      fetchContainers();
    }).catch(err => {
      alert(err.response?.data?.detail || 'Error stopping container');
    });
  };

  const handleRestart = async (instanceId) => {
    if (!window.confirm('Start this stopped container? It will reallocate its original GPUs.')) return;
    withProcessingId(instanceId, 'starting', async () => {
      await api.post(`/api/containers/${instanceId}/start`);
      alert('Container started');
      fetchContainers();
    }).catch(err => {
      alert(err.response?.data?.detail || 'Error starting container');
    });
  };

  const handleDelete = async (instanceId) => {
    if (!window.confirm('Delete this container permanently? This cannot be undone.')) return;
    withProcessingId(instanceId, 'deleting', async () => {
      await api.delete(`/api/containers/${instanceId}/remove`);
      alert('Container deleted');
      fetchContainers();
    }).catch(err => {
      alert(err.response?.data?.detail || 'Error deleting container');
    });
  };

  const handleRebuild = async (instanceId) => {
    if (!window.confirm('Rebuild this container from its saved snapshot and start it? Workspace data is preserved.')) return;
    withProcessingId(instanceId, 'rebuilding', async () => {
      await api.post(`/api/containers/${instanceId}/rebuild`);
      alert('Container rebuilt and started');
      fetchContainers();
    }).catch(err => {
      alert(err.response?.data?.detail || 'Error rebuilding container');
    });
  };

  const handleToggleProtection = async (instanceId, current) => {
    withProcessingId(instanceId, 'protecting', async () => {
      await api.put(`/api/containers/${instanceId}/protection`, { protected: !current });
      fetchContainers();
    }).catch(err => {
      alert(err.response?.data?.detail || 'Error updating protection');
    });
  };

  const statusColor = (s) => (
    s === 'running' ? '#52c41a' : s === 'removed' ? '#faad14' : s === 'error' ? '#ff4d4f' : '#888'
  );

  const selectedImage = images.find(img => img.id === parseInt(form.image_id));

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <h2>Register New Container</h2>
          <form onSubmit={handleStart} style={{ marginTop: 16 }}>
            <div className="form-group">
              <label>镜像:</label>
              <select
                value={form.image_id}
                onChange={(e) => setForm({ ...form, image_id: e.target.value })}
                required>
                <option value="">-- Select an image --</option>
                {images.map((img) => (
                  <option key={img.id} value={img.id}>
                    {img.name} ({img.image}){img.description ? ` - ${img.description}` : ''}
                  </option>
                ))}
              </select>
            </div>
            {selectedImage && (
              <p style={{ fontSize: 13, color: '#888', marginTop: -12, marginBottom: 16 }}>
                Min GPUs: {selectedImage.min_gpu} | Recommended: {selectedImage.recommended_gpu}
              </p>
            )}
            <div className="form-group">
              <label>GPU 数量:</label>
              <input type="number" min="1" max="4" value={form.gpu_count}
                onChange={(e) => setForm({ ...form, gpu_count: parseInt(e.target.value) || 1 })} />
            </div>
            <div className="form-group">
              <label>CPU 限制 (核心数, 可选):</label>
              <input type="number" step="0.5" min="0.5" placeholder="e.g., 4"
                value={form.cpu_limit}
                onChange={(e) => setForm({ ...form, cpu_limit: e.target.value })} />
            </div>
            <div className="form-group">
              <label>内存限制 (MB, 可选):</label>
              <input type="number" min="128" step="128" placeholder="e.g., 8192"
                value={form.memory_limit}
                onChange={(e) => setForm({ ...form, memory_limit: e.target.value })} />
            </div>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? 'Registering...' : 'Register Container'}
            </button>
          </form>
        </div>

        <div className="card">
          <h2 style={{ marginBottom: 4 }}>My Containers</h2>
          <p style={{ fontSize: 12, color: '#999', margin: '0 0 16px' }}>
            removed = 自动清理已删除容器、释放端口，可 <b>Rebuild</b> 一键还原启动（会分配新端口，旧 ssh 端口号失效）；
            保护 = 该容器不会进入自动清理候选。
          </p>
          {containers.length === 0 ? (
            <p style={{ color: '#888' }}>No containers running.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Image</th>
                  <th>服务器地址</th>
                  <th>GPUs</th>
                  <th>CPU</th>
                  <th>Memory</th>
                  <th>Password</th>
                  <th>Status</th>
                  <th>保护</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {containers.map(c => (
                  <tr key={c.id}>
                    <td style={{ fontFamily: 'monospace' }}>{c.container_id}</td>
                    <td>{c.image}</td>
                    <td style={{ fontFamily: 'monospace' }}>
                      {c.assigned_port ? `${hostIp || window.location.hostname}:${c.assigned_port}` : '-'}
                    </td>
                    <td>{c.gpu_count} (IDs: {c.gpu_ids?.join(',')})</td>
                    <td>{c.cpu_limit ? `${c.cpu_limit} cores` : 'Unlimited'}</td>
                    <td>{c.memory_limit ? `${c.memory_limit} MB` : 'Unlimited'}</td>
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
                      <span className="status-badge" style={{ background: statusColor(c.status) }}>
                        {c.status === 'removed' ? 'removed' : c.status}
                      </span>
                    </td>
                    <td>
                      <button
                        className={`protect-btn${c.cleanup_protected ? ' protected' : ''}`}
                        title={c.cleanup_protected ? '已保护：不会被自动清理' : '未保护：可能被自动清理'}
                        onClick={() => handleToggleProtection(c.id, c.cleanup_protected)}
                        disabled={!!processingIds[c.id]}>
                        {processingIds[c.id] === 'protecting'
                          ? '…'
                          : c.cleanup_protected ? '🔒 已保护' : '🔓 保护'}
                      </button>
                    </td>
                    <td>
                      {c.status === 'removed' && (
                        <button className="btn btn-primary" onClick={() => handleRebuild(c.id)}
                          disabled={!!processingIds[c.id]}
                          style={{ marginRight: 4 }}>
                          {processingIds[c.id] === 'rebuilding' ? 'Rebuilding...' : 'Rebuild'}
                        </button>
                      )}
                      {c.status === 'running' && (
                        <button className="btn btn-danger" onClick={() => handleStop(c.id)}
                          disabled={!!processingIds[c.id]}
                          style={{ marginRight: 4 }}>
                          {processingIds[c.id] === 'stopping' ? 'Stopping...' : 'Stop'}
                        </button>
                      )}
                      {c.status === 'stopped' && (
                        <button className="btn btn-primary" onClick={() => handleRestart(c.id)}
                          disabled={!!processingIds[c.id]}
                          style={{ marginRight: 4 }}>
                          {processingIds[c.id] === 'starting' ? 'Starting...' : 'Start'}
                        </button>
                      )}
                      <button className="btn btn-secondary"
                        onClick={() => handleDelete(c.id)}
                        disabled={!!processingIds[c.id]}>
                        {processingIds[c.id] === 'deleting' ? 'Deleting...' : 'Delete'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
};

export default ContainerManagement;
