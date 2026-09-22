import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import AppHeader from '../components/AppHeader';
import { useMode } from '../context/ModeContext';

const Guide = () => {
  const [user] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('user') || 'null');
    } catch {
      return null;
    }
  });
  const { mode, confirmed } = useMode();
  const isAdmin = user?.role === 'admin';
  const agentAvailable = confirmed && mode === 'llm';

  return (
    <div>
      <AppHeader user={user} />
      <main className="content home-page">
        <section className="home-hero" aria-labelledby="guide-title">
          <div className="home-hero-copy">
            <span className="home-eyebrow">GPU 资源管理平台</span>
            <h1 id="guide-title">使用指南</h1>
            <p>查看资源、使用容器或智能助手，保存好工作文件。</p>
          </div>
          <div className="home-workspace-preview" aria-label="容器工作目录提示">
            <span>容器内的工作目录</span>
            <code>/workspace</code>
            <p>这里挂载到宿主机工作区。代码、数据和结果请保存在这里，以便容器重建后继续使用。</p>
          </div>
        </section>

        <section className="home-section guide-first-section" aria-labelledby="guide-start-title">
          <div className="home-section-heading">
            <div>
              <span className="home-section-kicker">基本操作</span>
              <h2 id="guide-start-title">{isAdmin ? '管理平台资源' : '使用 GPU 容器'}</h2>
            </div>
          </div>
          <div className="home-steps guide-steps-three">
            <article className="home-step">
              <span className="home-step-number">01</span>
              <h3>查看资源</h3>
              <p>在 <Link to="/dashboard">GPU 状态</Link> 查看设备占用；{isAdmin ? <>在 <Link to="/admin/users">用户管理</Link> 设置配额。</> : <>在 <Link to="/profile">个人资料</Link> 查看自己的配额。</>}</p>
            </article>
            <article className="home-step">
              <span className="home-step-number">02</span>
              <h3>{isAdmin ? '管理镜像' : '创建容器'}</h3>
              <p>{isAdmin
                ? <>在 <Link to="/admin/images">镜像管理</Link> 查看或删除本地镜像。</>
                : <>在 <Link to="/containers">我的容器</Link> 把本地镜像加入个人预设，再设置 GPU、CPU 和内存。</>}</p>
            </article>
            <article className="home-step">
              <span className="home-step-number">03</span>
              <h3>{isAdmin ? '查看运行情况' : '连接并工作'}</h3>
              <p>{isAdmin
                ? <>在 <Link to="/dashboard">GPU 状态</Link> 查看资源使用情况；需要时前往清理与告警页面。</>
                : <>按“我的容器”页面提供的连接信息登录，随后进入工作目录。</>}</p>
            </article>
          </div>
          {!isAdmin && (
            <div className="guide-terminal" aria-label="SSH 连接与工作目录命令示例">
              <div className="guide-terminal-title">连接示例 · 用户、地址、端口和密码以容器页面为准</div>
              <div className="guide-terminal-body">
                <div><span>本机 $</span> {'ssh <SSH用户>@<服务器地址> -p <端口>'}</div>
                <div className="guide-terminal-hint">输入页面提供的密码</div>
                <div><span>容器 $</span> cd /workspace</div>
              </div>
            </div>
          )}
        </section>

        <section className="home-section" aria-labelledby="guide-agent-title">
          <div className="home-section-heading">
            <div>
              <span className="home-section-kicker">智能模式</span>
              <h2 id="guide-agent-title">使用智能助手</h2>
            </div>
          </div>
          <div className="guide-agent-panel">
            <p>在首页顶部切换到“智能模式”，进入“智能助手”。助手可通过对话完成手动模式中的所有平台操作，包括查询 GPU、管理镜像与容器。创建容器时，请写清镜像及明确要求的 GPU、CPU 和内存配置。</p>
            <p>{isAdmin
              ? '管理员还可让助手搜索 Docker Hub 镜像，选定候选后拉取。'
              : '执行后查看助手回复和工具状态，并在“我的容器”核对结果；需要新镜像或更多配额时请联系管理员。'}</p>
            <Link to={agentAvailable ? '/chat' : '/'}>{agentAvailable ? '打开智能助手 →' : '前往首页切换模式 →'}</Link>
          </div>
        </section>

        <section className="home-section" aria-labelledby="guide-notes-title">
          <div className="home-section-heading">
            <div>
              <span className="home-section-kicker">注意事项</span>
              <h2 id="guide-notes-title">镜像与容器</h2>
            </div>
          </div>
          <div className="home-notes guide-notes-two">
            <article>
              <h3>个人镜像预设</h3>
              <p>加入或移除预设只改变个人可选镜像，不会拉取或删除真实镜像。镜像拉取和删除由管理员处理。</p>
            </article>
            <article>
              <h3>停止与重建</h3>
              <p>停止容器会释放 GPU；重建后 SSH 端口可能变化。手动删除的容器无法恢复，请确认重要文件已存入工作区。</p>
            </article>
          </div>
        </section>
      </main>
    </div>
  );
};

export default Guide;
