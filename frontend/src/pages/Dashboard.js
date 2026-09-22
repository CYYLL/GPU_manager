import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';

const statusConfig = {
  free: { label: '空闲', color: '#52c41a', bg: '#f6ffed' },
  occupied: { label: '已占用', color: '#ff4d4f', bg: '#fff2f0' },
  stale: { label: '状态待核对', color: '#faad14', bg: '#fffbe6' },
  error: { label: '⚠ 异常', color: '#722ed1', bg: '#f9f0ff' },
};

const getMemoryClass = (pct) => {
  if (pct <= 30) return 'low';
  if (pct <= 70) return 'mid';
  return 'high';
};

const Dashboard = () => {
  const [gpuStatus, setGpuStatus] = useState([]);
  const [userInfo, setUserInfo] = useState(null);
  const [fetchError, setFetchError] = useState('');

  const fetchGpuStatus = useCallback(async () => {
    try {
      const response = await api.get('/api/gpus/status');
      setGpuStatus(response.data);
      setFetchError('');   // 成功即清除提示（5s 轮询会自动恢复）
    } catch (err) {
      console.error('Error fetching GPU status:', err);
      setFetchError('GPU 状态获取失败（服务器暂时不可用），将每 5 秒自动重试…');
    }
  }, []);

  useEffect(() => {
    fetchGpuStatus();
    const userStr = localStorage.getItem('user');
    if (userStr) setUserInfo(JSON.parse(userStr));
    const interval = setInterval(fetchGpuStatus, 5000);
    return () => clearInterval(interval);
  }, [fetchGpuStatus]);

  return (
    <div>
      <AppHeader user={userInfo} />
      <div className="content">
        {fetchError && (
          <div className="card" style={{ padding: '12px 16px', background: '#fff7e6', border: '1px solid #ffd591' }}>
            <span style={{ color: '#d46b08', fontSize: 13 }}>{fetchError}</span>
          </div>
        )}
        <div className="card" style={{ paddingBottom: 12 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <h2 style={{ margin: 0 }}>GPU 状态</h2>
            <div style={{ display: 'flex', gap: 20, fontSize: 14, color: '#888' }}>
              <span><span className="badge-dot" style={{ background: '#52c41a' }}></span>空闲</span>
              <span><span className="badge-dot" style={{ background: '#ff4d4f' }}></span>已占用</span>
              <span><span className="badge-dot" style={{ background: '#faad14' }}></span>待核对</span>
              <span><span className="badge-dot" style={{ background: '#722ed1' }}></span>异常</span>
              <span>每 5 秒自动刷新</span>
            </div>
          </div>
          <div className="gpu-grid">
            {gpuStatus.map((gpu) => {
              const cfg = statusConfig[gpu.status] || statusConfig.free;
              const memPct = gpu.memory_utilization;
              const utilPct = gpu.gpu_utilization;
              return (
                <div key={gpu.id} className={`gpu-card status-${gpu.status}`}>
                  {/* Header */}
                  <h3>
                    <span>GPU {gpu.id}</span>
                    <span className="gpu-name">{gpu.name}</span>
                    <span className="status-badge" style={{ background: cfg.color }}>
                      {cfg.label}
                    </span>
                  </h3>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    {gpu.error ? <span /> : <span className="gpu-temp">{gpu.temperature}°C</span>}
                    {gpu.allocated_to && <span className="gpu-user">使用者：{gpu.allocated_to}</span>}
                  </div>

                  {/* Memory bar */}
                  <div className="progress-group">
                    <div className="progress-label">
                      <span>显存</span>
                      <span>{gpu.used_memory} / {gpu.total_memory} GB ({memPct}%)</span>
                    </div>
                    <div className="progress-bar">
                      <div className={`progress-fill ${getMemoryClass(memPct)}`}
                        style={{ width: `${Math.min(memPct, 100)}%` }} />
                    </div>
                  </div>

                  {/* Utilization bar */}
                  <div className="progress-group gpu-util-bar">
                    <div className="progress-label">
                      <span>利用率</span>
                      <span>{utilPct}%</span>
                    </div>
                    <div className="progress-bar">
                      <div className="gpu-util-fill"
                        style={{ width: `${Math.min(utilPct, 100)}%` }} />
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
