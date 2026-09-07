import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';
import ModeToggle from '../components/ModeToggle';
import { useMode } from '../context/ModeContext';

const GB = 1024 ** 3;
const fmt = (bytes) => (bytes == null ? '-' : `${(bytes / GB).toFixed(2)} GiB`);
const timeOf = (ts) => (ts ? new Date(ts).toLocaleString() : '-');
const strOf = (v) => (v == null || v === 'None' || v === '' ? '-' : String(v));

const AdminCleanupLog = () => {
  const { mode, loading: modeLoading } = useMode();
  const [user, setUser] = useState(null);
  const [disk, setDisk] = useState(null);
  const [cands, setCands] = useState(null);
  const [log, setLog] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const llm = mode === 'llm';

  useEffect(() => {
    const u = localStorage.getItem('user');
    if (u) setUser(JSON.parse(u));
  }, []);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError('');
    const isLlm = mode === 'llm';
    try {
      if (isLlm) {
        const [d, c] = await Promise.all([
          api.get('/api/monitor/disk'),
          api.get('/api/monitor/candidates'),
        ]);
        setDisk(d.data);
        setCands(c.data);
      }
      const [lg, al] = await Promise.all([
        api.get('/api/admin/cleanup-log'),
        api.get('/api/admin/alerts'),
      ]);
      setLog(lg.data || []);
      setAlerts(al.data || []);
    } catch (err) {
      setError(err.response?.data?.detail || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [mode]);

  useEffect(() => {
    if (!modeLoading) fetchAll();
  }, [mode, modeLoading, fetchAll]);

  const handleResolve = async (id) => {
    try {
      await api.post(`/api/admin/alerts/${id}/resolve`);
      fetchAll();
    } catch (err) {
      setError(err.response?.data?.detail || '处理失败');
    }
  };

  const usageColor = (pct) => (pct > 90 ? '#ff4d4f' : pct > 75 ? '#faad14' : '#52c41a');

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0 }}>清理监控 / Admin Cleanup</h2>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <ModeToggle />
            <button className="btn btn-primary" onClick={fetchAll} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </div>
        {error && <div style={{ color: '#ff4d4f', margin: '8px 0' }}>{error}</div>}

        {/* llm-gated sections (monitor disk + candidates) */}
        {!llm && !modeLoading && (
          <div className="card" style={{ marginTop: 16 }}>
            <h3>磁盘与候选（需要 LLM 模式）</h3>
            <p style={{ color: '#888', fontSize: 13 }}>
              <code>/api/monitor/disk</code> 与 <code>/api/monitor/candidates</code> 仅 LLM 模式可读
              （传统模式 403）。切换到 LLM 模式查看磁盘水位与自动清理候选。
            </p>
            <ModeToggle />
          </div>
        )}

        {llm && (
          <>
            <div className="card" style={{ marginTop: 16 }}>
              <h3>磁盘水位</h3>
              {disk?.live && (
                <div style={{ margin: '8px 0' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: '#666' }}>
                    <span>实时用量 {disk.live.usage_percent.toFixed(1)}%（{fmt(disk.live.free_bytes)} 空闲 / {fmt(disk.live.total_bytes)}）</span>
                    <span style={{ color: '#999' }}>最新采样：{disk.latest ? timeOf(disk.latest.ts) : '暂无'}</span>
                  </div>
                  <div className="progress-bar" style={{ height: 10, background: '#eee', borderRadius: 5, overflow: 'hidden' }}>
                    <div className="progress-fill" style={{ width: `${Math.min(100, disk.live.usage_percent)}%`, background: usageColor(disk.live.usage_percent), height: '100%' }} />
                  </div>
                </div>
              )}
              {(disk?.trend || []).length > 0 && (
                <div style={{ margin: '10px 0' }}>
                  <div style={{ fontSize: 12, color: '#999', marginBottom: 4 }}>近 {disk.trend.length} 次采样趋势</div>
                  <div style={{ display: 'flex', alignItems: 'flex-end', gap: 2, height: 40 }}>
                    {disk.trend.map((s, i) => (
                      <div key={i}
                        title={`${timeOf(s.ts)} ${s.usage_percent.toFixed(1)}%`}
                        style={{ width: 10, height: `${Math.max(2, s.usage_percent)}%`, background: usageColor(s.usage_percent), borderRadius: 2, flexShrink: 0 }} />
                    ))}
                  </div>
                </div>
              )}
              <table>
                <thead><tr><th>项目</th><th>可回收空间</th></tr></thead>
                <tbody>
                  <tr><td>容器层（stopped SizeRw）</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_container_reclaimable_bytes) : '-'}</td></tr>
                  <tr><td>悬空镜像 SizeRootFs</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_image_reclaimable_bytes) : '-'}</td></tr>
                  <tr><td>构建缓存 BuildCache</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_build_cache_reclaimable_bytes) : '-'}</td></tr>
                </tbody>
              </table>
            </div>

            <div className="card" style={{ marginTop: 16 }}>
              <h3>自动清理候选（LRU，保护/宽限已排除）</h3>
              {cands?.params && (
                <p style={{ fontSize: 12, color: '#888', marginTop: 4 }}>
                  grace_days={cands.params.grace_days} · dry_run={String(cands.params.dry_run)}
                </p>
              )}
              <p style={{ fontSize: 12, color: '#faad14' }}>
                alert = 运行中容器，仅告警、绝不自动 stop/remove。
              </p>
              <table>
                <thead>
                  <tr><th>实例</th><th>镜像</th><th>状态</th><th>最后使用</th>
                    <th>可回收(GiB)</th><th>该用户容器</th><th>历史清理</th></tr>
                </thead>
                <tbody>
                  {(cands?.auto || []).map((c) => (
                    <tr key={c.id}>
                      <td>{c.id}</td><td>{c.image}</td><td>{c.status}</td>
                      <td>{strOf(c.last_used)}</td>
                      <td>{c.reclaim_bytes ? (c.reclaim_bytes / GB).toFixed(2) : '0'}</td>
                      <td>{c.user_container_count}</td><td>{c.prev_cleanup_count}</td>
                    </tr>
                  ))}
                  {(cands?.alert || []).map((c) => (
                    <tr key={`a${c.id}`} style={{ background: '#fffbe6' }}>
                      <td>{c.id}</td><td>{c.image}</td><td>⚠ running 仅告警</td>
                      <td>{strOf(c.last_used)}</td>
                      <td>{c.reclaim_bytes ? (c.reclaim_bytes / GB).toFixed(2) : '0'}</td>
                      <td>{c.user_container_count}</td><td>{c.prev_cleanup_count}</td>
                    </tr>
                  ))}
                  {cands && cands.auto.length === 0 && cands.alert.length === 0 && (
                    <tr><td colSpan="7" style={{ color: '#999' }}>暂无候选</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}

        <div className="card" style={{ marginTop: 16 }}>
          <h3>自动清理日志（最近 200 条）</h3>
          <table>
            <thead>
              <tr><th>时间</th><th>实例</th><th>用户</th><th>动作</th><th>来源</th>
                <th>原因</th><th>释放(GiB)</th><th>成功</th></tr>
            </thead>
            <tbody>
              {log.map((r) => (
                <tr key={r.id}>
                  <td>{timeOf(r.ts)}</td>
                  <td>{r.container_instance_id ?? '-'}</td>
                  <td>{r.user_id ?? '-'}</td>
                  <td>{r.action}</td>
                  <td>{r.decision_source}</td>
                  <td style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{strOf(r.reason)}</td>
                  <td>{r.freed_bytes ? (r.freed_bytes / GB).toFixed(2) : '0'}</td>
                  <td>{r.success ? '✓' : '✗'}</td>
                </tr>
              ))}
              {log.length === 0 && <tr><td colSpan="8" style={{ color: '#999' }}>暂无清理记录</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="card" style={{ marginTop: 16 }}>
          <h3>容量/系统告警（未解决在前）</h3>
          <table>
            <thead><tr><th>时间</th><th>级别</th><th>类型</th><th>消息</th><th>meta</th><th>操作</th></tr></thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id} style={{ background: a.resolved_at ? undefined : '#fffbe6' }}>
                  <td>{timeOf(a.ts)}</td>
                  <td>
                    <span className="status-badge"
                      style={{ background: a.level === 'critical' ? '#ff4d4f' : '#faad14' }}>
                      {a.level}
                    </span>
                  </td>
                  <td>{a.type}</td>
                  <td>{strOf(a.message)}</td>
                  <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {a.meta && Object.keys(a.meta).length ? JSON.stringify(a.meta) : '-'}
                  </td>
                  <td>
                    {a.resolved_at ? (
                      <span style={{ color: '#999', fontSize: 13 }}>已处理 {timeOf(a.resolved_at)}</span>
                    ) : (
                      <button className="btn btn-primary" onClick={() => handleResolve(a.id)}>处理</button>
                    )}
                  </td>
                </tr>
              ))}
              {alerts.length === 0 && <tr><td colSpan="6" style={{ color: '#999' }}>暂无告警</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

export default AdminCleanupLog;
