import { apiErrorText } from '../services/errorMessages';
import React, { useState, useEffect, useCallback } from 'react';
import api from '../services/api';
import AppHeader from '../components/AppHeader';
import { useMode } from '../context/ModeContext';

const GB = 1024 ** 3;
const fmt = (bytes) => (bytes == null ? '-' : `${(bytes / GB).toFixed(2)} GiB`);
const timeOf = (ts) => (ts ? new Date(ts).toLocaleString() : '-');
const strOf = (v) => (v == null || v === 'None' || v === '' ? '-' : String(v));
const statusLabel = { running: '运行中', stopped: '已停止', removed: '已移除', error: '异常' };
const actionLabel = { remove: '删除', skip: '跳过' };
const sourceLabel = { llm: '智能助手', rule_fallback: '规则兜底', manual: '手动', agent: '系统' };
const alertTypeLabel = { capacity: '容量', gpu_conflict: 'GPU 冲突' };
const alertLevelLabel = { warning: '警告', critical: '严重' };
const alertDetail = (alert) => {
  if (!alert.meta || Object.keys(alert.meta).length === 0) return '-';
  if (alert.type === 'capacity' && alert.meta.source_freed != null) {
    return `源头估算回收：${alert.meta.source_freed} 字节`;
  }
  if (alert.type === 'gpu_conflict' && alert.meta.conflicts) {
    return alert.meta.conflicts.map(item => `GPU ${item.gpu_id}：${item.containers.map(c => c.name).join('、')}`).join('；');
  }
  return JSON.stringify(alert.meta);
};

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
      setError(apiErrorText(err, '加载失败'));
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
      setError(apiErrorText(err, '处理失败'));
    }
  };

  const usageColor = (pct) => (pct > 90 ? '#ff4d4f' : pct > 75 ? '#faad14' : '#52c41a');

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0 }}>清理与系统告警</h2>
          <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
            <button className="btn btn-primary" onClick={fetchAll} disabled={loading}>
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </div>
        {error && <div style={{ color: '#ff4d4f', margin: '8px 0' }}>{error}</div>}

        {/* llm-gated sections (monitor disk + candidates) */}
        {!llm && !modeLoading && (
          <div className="card" style={{ marginTop: 16 }}>
            <h3>磁盘与候选（需要智能模式）</h3>
            <p style={{ color: '#888', fontSize: 13 }}>
              <code>/api/monitor/disk</code> 与 <code>/api/monitor/candidates</code> 仅智能模式可读
              。模式开关仅在平台首页可用——
              切到智能模式后再返回本页查看磁盘水位与自动清理候选。
            </p>
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
                  <tr><td>已停止容器的可写层</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_container_reclaimable_bytes) : '-'}</td></tr>
                  <tr><td>悬空镜像层</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_image_reclaimable_bytes) : '-'}</td></tr>
                  <tr><td>构建缓存</td>
                    <td>{disk?.latest ? fmt(disk.latest.docker_build_cache_reclaimable_bytes) : '-'}</td></tr>
                </tbody>
              </table>
            </div>

            <div className="card" style={{ marginTop: 16 }}>
              <h3>自动清理候选（按最近使用时间排序，已排除保护和宽限期内的容器）</h3>
              {cands?.params && (
                <p style={{ fontSize: 12, color: '#888', marginTop: 4 }}>
                  宽限期：{cands.params.grace_days} 天 · 模拟运行：{cands.params.dry_run ? '是' : '否'}
                </p>
              )}
              <p style={{ fontSize: 12, color: '#faad14' }}>
                运行中的容器只告警，不会自动停止或删除。
              </p>
              <table>
                <thead>
                  <tr><th>实例</th><th>镜像</th><th>状态</th><th>最后使用</th>
                    <th>可回收空间（GiB）</th><th>该用户容器数</th><th>历史清理次数</th></tr>
                </thead>
                <tbody>
                  {(cands?.auto || []).map((c) => (
                    <tr key={c.id}>
                      <td>{c.id}</td><td>{c.image}</td><td>{statusLabel[c.status] || '未知'}</td>
                      <td>{strOf(c.last_used)}</td>
                      <td>{c.reclaim_bytes ? (c.reclaim_bytes / GB).toFixed(2) : '0'}</td>
                      <td>{c.user_container_count}</td><td>{c.prev_cleanup_count}</td>
                    </tr>
                  ))}
                  {(cands?.alert || []).map((c) => (
                    <tr key={`a${c.id}`} style={{ background: '#fffbe6' }}>
                      <td>{c.id}</td><td>{c.image}</td><td>⚠ 运行中，仅告警</td>
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
                <th>原因</th><th>释放空间（GiB）</th><th>结果</th></tr>
            </thead>
            <tbody>
              {log.map((r) => (
                <tr key={r.id}>
                  <td>{timeOf(r.ts)}</td>
                  <td>{r.container_instance_id ?? '-'}</td>
                  <td>{r.user_id ?? '-'}</td>
                  <td>{actionLabel[r.action] || r.action}</td>
                  <td>{sourceLabel[r.decision_source] || r.decision_source}</td>
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
            <thead><tr><th>时间</th><th>级别</th><th>类型</th><th>消息</th><th>详情</th><th>操作</th></tr></thead>
            <tbody>
              {alerts.map((a) => (
                <tr key={a.id} style={{ background: a.resolved_at ? undefined : '#fffbe6' }}>
                  <td>{timeOf(a.ts)}</td>
                  <td>
                    <span className="status-badge"
                      style={{ background: a.level === 'critical' ? '#ff4d4f' : '#faad14' }}>
                      {alertLevelLabel[a.level] || a.level}
                    </span>
                  </td>
                  <td>{alertTypeLabel[a.type] || a.type}</td>
                  <td>{strOf(a.message)}</td>
                  <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {alertDetail(a)}
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
