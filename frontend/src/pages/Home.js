import React, { useState, useEffect } from 'react';
import AppHeader from '../components/AppHeader';

const Home = () => {
  const [user, setUser] = useState(null);

  useEffect(() => {
    const userStr = localStorage.getItem('user');
    if (userStr) setUser(JSON.parse(userStr));
  }, []);

  return (
    <div>
      <AppHeader user={user} />
      <div className="content">
        <div className="card">
          <h2 style={{ marginBottom: 8 }}>Welcome / 欢迎</h2>
          <p style={{ color: '#888', fontSize: 14 }}>
            GPU Resource Manager — allocate and monitor GPU resources across your team.
            GPU 资源管理 — 为团队分配和监控 GPU 资源。
          </p>
          <p style={{ color: '#aaa', fontSize: 12, marginTop: 8 }}>
            Navigation items are shown based on your role. Admin features are only visible to admin users.
            导航项根据您的角色显示，管理员功能仅对 admin 用户可见。
          </p>
        </div>

        <div className="card guide-card">
          <h2 style={{ marginBottom: 16 }}>Navigation Guide / 导航指南</h2>

          <div className="guide-item">
            <span className="guide-icon">📊</span>
            <div>
              <strong>Dashboard</strong>
              <p className="guide-en">View real-time GPU status — memory usage, utilization, temperature, and allocation status (FREE / OCCUPIED / STALE / ERROR).</p>
              <p className="guide-zh">查看 GPU 实时状态 — 显存使用、利用率、温度、以及分配状态（空闲/占用中/悬空/异常）。</p>
            </div>
          </div>

          {user?.role !== 'admin' && (
          <div className="guide-item">
            <span className="guide-icon">🐳</span>
            <div>
              <strong>Containers</strong>
              <p className="guide-en">Start new GPU containers with preset images. Configure GPU count, CPU cores, and memory limits. <b>One container per user</b> — stop the current one before starting a new one. New users have <b>GPU quota = 0</b> until assigned by an admin.</p>
              <p className="guide-zh">使用预置镜像启动 GPU 容器。配置 GPU 数量、CPU 核数和内存限制。<b>同一用户只能启动一个容器</b>— 需先停止当前容器才能启动新容器。新用户<b>GPU 配额默认为 0</b>，需管理员分配后方可使用。</p>
            </div>
          </div>
          )}

          <div className="guide-item">
            <span className="guide-icon">👤</span>
            <div>
              <strong>Profile</strong>
              {user?.role === 'admin' ? (
                <>
                  <p className="guide-en">Manage system settings — reset user passwords and view your account info.</p>
                  <p className="guide-zh">管理系统设置 — 重置用户密码和查看个人账户信息。</p>
                </>
              ) : (
                <>
                  <p className="guide-en">View your personal GPU quota, current usage, container history, and change your password.</p>
                  <p className="guide-zh">查看个人 GPU 配额、当前用量、历史容器记录，以及修改密码。</p>
                </>
              )}
            </div>
          </div>

          {user?.role === 'admin' && (
            <div className="guide-item">
              <span className="guide-icon">⚙️</span>
              <div>
                <strong>Admin</strong>
                <p className="guide-en">Manage users (GPU quotas, containers, delete users), manage preset Docker images (add/delete), and view the container mount root path.</p>
                <p className="guide-zh">管理用户（GPU 配额、容器、删除用户）、管理预置 Docker 镜像（添加/删除）、查看容器挂载根目录。</p>
              </div>
            </div>
          )}

          <div className="guide-item">
            <span className="guide-icon">🎯</span>
            <div>
              <strong>GPU Status Legend / 状态图例</strong>
              <p className="guide-en">
                <span className="badge-dot" style={{ background: '#52c41a' }}></span> <b>FREE</b> — Available for allocation<br />
                <span className="badge-dot" style={{ background: '#ff4d4f' }}></span> <b>OCCUPIED</b> — In use (container running + GPU active)<br />
                <span className="badge-dot" style={{ background: '#faad14' }}></span> <b>STALE</b> — DB record exists but container is stopped (needs cleanup)<br />
                <span className="badge-dot" style={{ background: '#722ed1' }}></span> <b>ERROR</b> — GPU is unavailable / hardware failure
              </p>
              <p className="guide-zh">
                <span className="badge-dot" style={{ background: '#52c41a' }}></span> <b>空闲</b> — 可分配使用<br />
                <span className="badge-dot" style={{ background: '#ff4d4f' }}></span> <b>占用中</b> — 正在使用（容器运行中 + GPU 活跃）<br />
                <span className="badge-dot" style={{ background: '#faad14' }}></span> <b>悬空</b> — 数据库有记录但容器已停止（需清理）<br />
                <span className="badge-dot" style={{ background: '#722ed1' }}></span> <b>异常</b> — GPU 不可用 / 硬件故障
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Home;
