import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const AdminImages = () => {
  const [images, setImages] = useState([]);
  const [user, setUser] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [presetPending, setPresetPending] = useState({});

  const fetchImages = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get('/api/images/available');
      setImages(res.data);
      setError('');
    } catch (err) {
      setError(err.response?.data?.detail || '加载本地镜像失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchImages();
    const userStr = localStorage.getItem('user');
    if (userStr) setUser(JSON.parse(userStr));
  }, [fetchImages]);

  const handleToggle = async (image) => {
    setPresetPending((prev) => ({ ...prev, [image.image_ref]: true }));
    try {
      await api.put('/api/images/presets', {
        image_ref: image.image_ref, selected: !image.selected,
      });
      await fetchImages();
    } catch (err) {
      alert(err.response?.data?.detail || '更新预设失败');
    } finally {
      setPresetPending((prev) => {
        const next = { ...prev };
        delete next[image.image_ref];
        return next;
      });
    }
  };

  const handleDeleteLocal = async (image) => {
    if (!window.confirm(`删除 Docker 本地镜像 ${image.image_ref}？所有用户的对应预设也会移除；被容器使用时会拒绝。`)) return;
    try {
      const res = await api.delete(`/api/admin/images/${image.id}/local`);
      alert(res.data?.message || '本地镜像已删除');
      fetchImages();
    } catch (err) {
      alert(err.response?.data?.detail || '删除本地镜像失败');
    }
  };

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
            <h2>本地 Docker 镜像 ({images.length})</h2>
            <button className="btn btn-secondary" onClick={fetchImages} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
          <p style={{ color: '#888', fontSize: 13, marginTop: 8 }}>
            每位用户自行选择预设；此处的加入或移出只影响管理员账号。管理员可在 Agent 中拉取镜像，并在此删除本地镜像。
          </p>
          {error && <p style={{ color: '#ff4d4f', marginTop: 12 }}>{error}</p>}
          {!error && images.length === 0 && <p style={{ marginTop: 16, color: '#888' }}>本地暂无镜像。</p>}
          {images.length > 0 && (
            <div style={{ overflowX: 'auto', marginTop: 16 }}>
              <table>
                <thead><tr><th>ID</th><th>镜像</th><th>大小</th><th>我的预设</th><th>操作</th></tr></thead>
                <tbody>
                  {images.map((image) => (
                    <tr key={image.image_ref}>
                      <td>{image.id}</td>
                      <td style={{ fontFamily: 'monospace' }}>{image.image_ref}</td>
                      <td>{image.size_bytes == null ? '-' : `${(image.size_bytes / (1024 ** 3)).toFixed(2)} GiB`}</td>
                      <td>{image.selected ? '已加入' : '未加入'}</td>
                      <td>
                        <button
                          className={`btn ${image.selected ? 'btn-preset-remove' : 'btn-preset-add'}`}
                          onClick={() => handleToggle(image)}
                          disabled={!!presetPending[image.image_ref]}
                          aria-pressed={image.selected}>
                          {presetPending[image.image_ref] ? '处理中…' : image.selected ? '− 移出预设' : '+ 加入预设'}
                        </button>
                        <button className="btn btn-danger" style={{ marginLeft: 8 }}
                          onClick={() => handleDeleteLocal(image)}>删除本地镜像</button>
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

export default AdminImages;
