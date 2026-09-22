import { apiErrorText } from '../services/errorMessages';
import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const ContainerManagement = () => {
  const [containers, setContainers] = useState([]);
  const [images, setImages] = useState([]);
  const [availableImages, setAvailableImages] = useState([]);
  const [imageError, setImageError] = useState('');
  const [presetPending, setPresetPending] = useState({});
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
    fetchAvailableImages();
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

  const fetchAvailableImages = async () => {
    try {
      const res = await api.get('/api/images/available');
      setAvailableImages(res.data);
      setImageError('');
    } catch (err) {
      setImageError(apiErrorText(err, '无法获取本地镜像列表'));
    }
  };

  const handlePresetToggle = async (image) => {
    setPresetPending((prev) => ({ ...prev, [image.image_ref]: true }));
    try {
      await api.put('/api/images/presets', {
        image_ref: image.image_ref, selected: !image.selected,
      });
      await Promise.all([fetchImages(), fetchAvailableImages()]);
      if (image.selected && String(form.image_id) === String(image.id)) {
        setForm((prev) => ({ ...prev, image_id: '' }));
      }
    } catch (err) {
      alert(apiErrorText(err, '更新镜像预设失败'));
    } finally {
      setPresetPending((prev) => {
        const next = { ...prev };
        delete next[image.image_ref];
        return next;
      });
    }
  };

  const handleStart = async (e) => {
    e.preventDefault();
    if (!form.image_id) return alert('请选择镜像');
    const gpuCount = Number(form.gpu_count);
    if (!Number.isInteger(gpuCount) || gpuCount < 1 || gpuCount > 4) {
      return alert('系统不支持创建 0 张 GPU 的容器，请设置 1 至 4 张 GPU');
    }
    setLoading(true);
    try {
      await api.post('/api/containers/start', {
        image_id: parseInt(form.image_id),
        gpu_count: gpuCount,
        cpu_limit: form.cpu_limit ? parseFloat(form.cpu_limit) : null,
        memory_limit: form.memory_limit ? parseInt(form.memory_limit) : null,
      });
      alert('容器创建成功');
      setForm({ image_id: '', gpu_count: 1, cpu_limit: '', memory_limit: '' });
      fetchContainers();
    } catch (err) {
      alert(apiErrorText(err, '创建容器失败'));
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
    if (!window.confirm('确定停止该容器吗？停止后会释放其 GPU 资源。')) return;
    withProcessingId(instanceId, 'stopping', async () => {
      await api.delete(`/api/containers/${instanceId}`);
      alert('容器已停止');
      fetchContainers();
    }).catch(err => {
      alert(apiErrorText(err, '停止容器失败'));
    });
  };

  const handleRestart = async (instanceId) => {
    if (!window.confirm('确定启动该容器吗？系统将重新分配其原有 GPU。')) return;
    withProcessingId(instanceId, 'starting', async () => {
      await api.post(`/api/containers/${instanceId}/start`);
      alert('容器已启动');
      fetchContainers();
    }).catch(err => {
      alert(apiErrorText(err, '启动容器失败'));
    });
  };

  const handleDelete = async (instanceId) => {
    if (!window.confirm('确定永久删除该容器吗？此操作无法撤销，工作区挂载目录不会删除。')) return;
    withProcessingId(instanceId, 'deleting', async () => {
      await api.delete(`/api/containers/${instanceId}/remove`);
      alert('容器已删除');
      fetchContainers();
    }).catch(err => {
      alert(apiErrorText(err, '删除容器失败'));
    });
  };

  const handleRebuild = async (instanceId) => {
    if (!window.confirm('确定根据保存的配置重建并启动容器吗？工作区中的数据会保留。')) return;
    withProcessingId(instanceId, 'rebuilding', async () => {
      await api.post(`/api/containers/${instanceId}/rebuild`);
      alert('容器已重建并启动');
      fetchContainers();
    }).catch(err => {
      alert(apiErrorText(err, '重建容器失败'));
    });
  };

  const handleToggleProtection = async (instanceId, current) => {
    withProcessingId(instanceId, 'protecting', async () => {
      await api.put(`/api/containers/${instanceId}/protection`, { protected: !current });
      fetchContainers();
    }).catch(err => {
      alert(apiErrorText(err, '更新保护状态失败'));
    });
  };

  const statusTone = (status) => (
    ['running', 'stopped', 'removed', 'error'].includes(status) ? status : 'other'
  );
  const statusLabel = {
    running: '运行中', stopped: '已停止', removed: '已移除', error: '异常',
  };

  const selectedImage = images.find(img => img.id === parseInt(form.image_id));

  return (
    <div>
      <AppHeader user={user} />
      <div className="content containers-page">
        <div className="card">
          <h2>我的镜像预设</h2>
          <p style={{ color: '#888', fontSize: 13, marginTop: 8 }}>
            从服务器已有镜像中选择。加入或移出只影响你的容器创建列表；拉取和删除实际镜像由管理员操作。
          </p>
          {imageError && <p style={{ color: '#ff4d4f', marginTop: 12 }}>{imageError}</p>}
          {!imageError && availableImages.length === 0 && (
            <p style={{ color: '#888', marginTop: 12 }}>本地暂无镜像。</p>
          )}
          {availableImages.length > 0 && (
            <div className="containers-table-scroll" style={{ marginTop: 16 }}>
              <table className="containers-presets-table">
                <thead><tr><th>镜像</th><th>大小</th><th>状态</th><th>操作</th></tr></thead>
                <tbody>
                  {availableImages.map((image) => (
                    <tr key={image.image_ref}>
                      <td className="containers-image-ref">{image.image_ref}</td>
                      <td>{image.size_bytes == null ? '-' : `${(image.size_bytes / (1024 ** 3)).toFixed(2)} GiB`}</td>
                      <td>{image.selected ? '已加入' : '未加入'}</td>
                      <td><button
                        className={`btn ${image.selected ? 'btn-preset-remove' : 'btn-preset-add'}`}
                        onClick={() => handlePresetToggle(image)}
                        disabled={!!presetPending[image.image_ref]}
                        aria-pressed={image.selected}>
                        {presetPending[image.image_ref] ? '处理中…' : image.selected ? '− 移出我的预设' : '+ 加入我的预设'}
                      </button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        <div className="card">
          <h2>创建新容器</h2>
          <form onSubmit={handleStart} style={{ marginTop: 16 }}>
            <div className="form-group">
              <label>镜像:</label>
              <select
                value={form.image_id}
                onChange={(e) => setForm({ ...form, image_id: e.target.value })}
                required>
                <option value="">请选择镜像</option>
                {images.map((img) => (
                  <option key={img.id} value={img.id}>
                    {img.name} ({img.image}){img.description ? ` - ${img.description}` : ''}
                  </option>
                ))}
              </select>
            </div>
            {selectedImage && (
              <p style={{ fontSize: 13, color: '#888', marginTop: -12, marginBottom: 16 }}>
                最少 GPU：{selectedImage.min_gpu} 张｜推荐：{selectedImage.recommended_gpu} 张
              </p>
            )}
            <div className="form-group">
              <label>GPU 数量:</label>
              <input type="number" min="1" max="4" value={form.gpu_count}
                onChange={(e) => setForm({ ...form, gpu_count: e.target.value })} />
            </div>
            <div className="form-group">
              <label>CPU 限制 (核心数, 可选):</label>
              <input type="number" step="0.5" min="0.5" placeholder="例如：4"
                value={form.cpu_limit}
                onChange={(e) => setForm({ ...form, cpu_limit: e.target.value })} />
            </div>
            <div className="form-group">
              <label>内存限制 (MB, 可选):</label>
              <input type="number" min="128" step="128" placeholder="例如：8192"
                value={form.memory_limit}
                onChange={(e) => setForm({ ...form, memory_limit: e.target.value })} />
            </div>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? '创建中…' : '创建容器'}
            </button>
          </form>
        </div>

        <div className="card">
          <h2 style={{ marginBottom: 4 }}>我的容器</h2>
          <p style={{ fontSize: 12, color: '#999', margin: '0 0 16px' }}>
            已移除 = 容器已删除、端口已释放，可通过“重建”按保存的配置再次启动（可能分配新端口，旧 SSH 端口失效）；
            保护 = 该容器不会进入自动清理候选。
          </p>
          {containers.length === 0 ? (
            <p style={{ color: '#888' }}>暂无容器。</p>
          ) : (
            <div className="containers-table-scroll" role="region" aria-label="容器列表" tabIndex="0">
              <table className="containers-list-table">
              <thead>
                <tr>
                  <th>容器编号</th>
                  <th>镜像</th>
                  <th>服务器地址</th>
                  <th>SSH 用户</th>
                  <th>GPU</th>
                  <th>CPU</th>
                  <th>内存</th>
                  <th>密码</th>
                  <th>状态</th>
                  <th>保护</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {containers.map(c => (
                  <tr key={c.id}>
                    <td style={{ fontFamily: 'monospace' }}>{c.container_id}</td>
                    <td className="containers-image-ref" title={c.image}>{c.image}</td>
                    <td className="containers-address">
                      {c.assigned_port ? `${hostIp || window.location.hostname}:${c.assigned_port}` : '-'}
                    </td>
                    <td style={{ fontFamily: 'monospace' }}>
                      {c.ssh_username || '未确认'}
                    </td>
                    <td>{c.gpu_count} 张（编号：{c.gpu_ids?.join(',')}）</td>
                    <td>{c.cpu_limit ? `${c.cpu_limit} 核` : '不限制'}</td>
                    <td>{c.memory_limit ? `${c.memory_limit} MB` : '不限制'}</td>
                    <td>
                      {c.access_password ? (
                        <button type="button" className="container-password-copy"
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
                          title="复制访问密码" aria-label="复制访问密码">
                          复制
                        </button>
                      ) : '-'}
                    </td>
                    <td>
                      <span className={`container-status container-status-${statusTone(c.status)}`}>
                        {statusLabel[c.status] || '未知'}
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
                      <div className="containers-row-actions">
                        {c.status === 'removed' && (
                          <button className="btn btn-primary" onClick={() => handleRebuild(c.id)}
                            disabled={!!processingIds[c.id]}>
                            {processingIds[c.id] === 'rebuilding' ? '重建中…' : '重建'}
                          </button>
                        )}
                        {c.status === 'running' && (
                          <button className="btn btn-danger" onClick={() => handleStop(c.id)}
                            disabled={!!processingIds[c.id]}>
                            {processingIds[c.id] === 'stopping' ? '停止中…' : '停止'}
                          </button>
                        )}
                        {c.status === 'stopped' && (
                          <button className="btn btn-primary" onClick={() => handleRestart(c.id)}
                            disabled={!!processingIds[c.id]}>
                            {processingIds[c.id] === 'starting' ? '启动中…' : '启动'}
                          </button>
                        )}
                        <button className="btn btn-secondary"
                          onClick={() => handleDelete(c.id)}
                          disabled={!!processingIds[c.id]}>
                          {processingIds[c.id] === 'deleting' ? '删除中…' : '删除'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default ContainerManagement;
