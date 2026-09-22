import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import AppHeader from '../components/AppHeader';
import { useMode } from '../context/ModeContext';

const Home = () => {
  const [user] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('user') || 'null');
    } catch {
      return null;
    }
  });
  const { mode, confirmed } = useMode();
  const isAdmin = user?.role === 'admin';

  return (
    <div>
      <AppHeader user={user} />
      <main className="content home-page">
        <section className="home-hero home-overview-hero" aria-labelledby="home-title">
          <div className="home-hero-copy">
            <span className="home-eyebrow">GPU 资源管理平台</span>
            <h1 id="home-title">让 GPU 资源和容器使用更清晰</h1>
            <p>在一个平台查看 GPU 状态、管理容器、选择本地镜像，并找到自己的工作区。</p>
          </div>
        </section>

        <section className="home-section" aria-labelledby="purpose-title">
          <div className="home-section-heading">
            <div>
              <span className="home-section-kicker">平台用途</span>
              <h2 id="purpose-title">你可以在这里做什么</h2>
            </div>
          </div>
          <div className={`home-purpose-grid${confirmed && mode === 'llm' ? '' : ' home-purpose-grid-three'}`}>
            <article>
              <h3>了解使用方式</h3>
              <p>查阅配额、镜像、容器连接和工作数据的使用方法。</p>
              <Link to="/guide">阅读使用指南 →</Link>
            </article>
            <article>
              <h3>查看 GPU 资源</h3>
              <p>了解各张 GPU 的占用、显存、使用率和温度，判断当前可用资源。</p>
              <Link to="/dashboard">查看 GPU 状态 →</Link>
            </article>
            {confirmed && mode === 'llm' && (
              <article>
                <h3>使用智能助手</h3>
                <p>通过对话查询资源、选择镜像，并协助完成容器相关操作。</p>
                <Link to="/chat">打开智能助手 →</Link>
              </article>
            )}
            <article>
              <h3>{isAdmin ? '管理平台资源' : '管理个人容器'}</h3>
              <p>{isAdmin ? '管理用户与镜像，并查看系统告警。' : '选择镜像、创建容器、查看连接信息，并按需停止或重新启动。'}</p>
              <Link to={isAdmin ? '/admin/users' : '/containers'}>{isAdmin ? '进入用户管理 →' : '管理我的容器 →'}</Link>
            </article>
          </div>
        </section>
      </main>
    </div>
  );
};

export default Home;
