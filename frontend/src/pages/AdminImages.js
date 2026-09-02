import React, { useState, useEffect } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const AdminImages = () => {
  const [images, setImages] = useState([]);
  const [user, setUser] = useState(null);
  const [mountRoot, setMountRoot] = useState('');
  const [form, setForm] = useState({
    name: '', image: '', description: '', min_gpu: 1, recommended_gpu: 1,
  });

  useEffect(() => {
    fetchImages();
    fetchMountRoot();
    const userStr = localStorage.getItem('user');
    if (userStr) setUser(JSON.parse(userStr));
  }, []);

  const fetchMountRoot = async () => {
    try {
      const res = await api.get('/api/admin/config/mount-root');
      setMountRoot(res.data.mount_root);
    } catch (err) {
      console.error('Failed to fetch mount root:', err);
    }
  };

  const fetchImages = async () => {
    try {
      const res = await api.get('/api/images');
      setImages(res.data);
    } catch (err) {
      console.error(err);
    }
  };

  const handleAdd = async (e) => {
    e.preventDefault();
    try {
      await api.post('/api/admin/images', form);
      alert('Image added');
      setForm({ name: '', image: '', description: '', min_gpu: 1, recommended_gpu: 1 });
      fetchImages();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error adding image');
    }
  };

  const handleDelete = async (imageId) => {
    if (!window.confirm('Delete this image?')) return;
    try {
      await api.delete(`/api/admin/images/${imageId}`);
      fetchImages();
    } catch (err) {
      alert(err.response?.data?.detail || 'Error deleting image');
    }
  };

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <h2>Add Preset Image</h2>
          <form onSubmit={handleAdd} style={{ marginTop: 16 }}>
            <div className="form-group">
              <label>Display Name:</label>
              <input type="text" placeholder="e.g., PyTorch 2.1" required
                value={form.name} onChange={e => setForm({...form, name: e.target.value})} />
            </div>
            <div className="form-group">
              <label>Docker Image:</label>
              <input type="text" placeholder="e.g., pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime" required
                value={form.image} onChange={e => setForm({...form, image: e.target.value})} />
            </div>
            <div className="form-group">
              <label>Description:</label>
              <textarea placeholder="Optional description"
                value={form.description} onChange={e => setForm({...form, description: e.target.value})} />
            </div>
            <div style={{ display: 'flex', gap: 16 }}>
              <div className="form-group" style={{ flex: 1 }}>
                <label>Min GPUs:</label>
                <input type="number" min="1" max="8" value={form.min_gpu}
                  onChange={e => setForm({...form, min_gpu: parseInt(e.target.value) || 1})} />
              </div>
              <div className="form-group" style={{ flex: 1 }}>
                <label>Recommended GPUs:</label>
                <input type="number" min="1" max="8" value={form.recommended_gpu}
                  onChange={e => setForm({...form, recommended_gpu: parseInt(e.target.value) || 1})} />
              </div>
            </div>
            <button type="submit" className="btn btn-primary">Add Image</button>
          </form>
        </div>

        {mountRoot && (
          <div className="card" style={{ padding: '12px 24px' }}>
            <span style={{ fontSize: 14, color: '#555' }}>
              <strong>Container Mount Root:</strong>{' '}
              <code style={{ color: '#000', fontWeight: 600, fontSize: 15 }}>{mountRoot}</code>
              <span style={{ marginLeft: 12, fontSize: 12, color: '#999' }}>
                (configurable in <code>.env</code> file)
              </span>
            </span>
          </div>
        )}

        <div className="card">
          <h2>Preset Images ({images.length})</h2>
          {images.length === 0 ? (
            <p style={{ marginTop: 16, color: '#888' }}>No images configured.</p>
          ) : (
            <table style={{ marginTop: 16 }}>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Name</th>
                  <th>Image</th>
                  <th>Min GPU</th>
                  <th>Rec. GPU</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {images.map(img => (
                  <tr key={img.id}>
                    <td>{img.id}</td>
                    <td>{img.name}</td>
                    <td style={{ fontFamily: 'monospace', fontSize: 13 }}>{img.image}</td>
                    <td>{img.min_gpu}</td>
                    <td>{img.recommended_gpu}</td>
                    <td>
                      <button className="btn btn-danger"
                        onClick={() => handleDelete(img.id)}>Delete</button>
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

export default AdminImages;
