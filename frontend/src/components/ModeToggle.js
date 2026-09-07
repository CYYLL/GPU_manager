import React from 'react';
import { useMode } from '../context/ModeContext';

// 顶部模式开关：LLM（agent 助手）/ 传统（手动容器管理）。
const ModeToggle = ({ compact }) => {
  const { mode, setMode } = useMode();
  return (
    <div className={`mode-toggle${compact ? ' mode-toggle-compact' : ''}`}
      title="交互模式：LLM 模式用 Agent 助手管理容器；传统模式手动操作">
      <button
        type="button"
        className={mode === 'llm' ? 'active' : ''}
        onClick={() => setMode('llm')}
        aria-pressed={mode === 'llm'}>
        LLM
      </button>
      <button
        type="button"
        className={mode === 'traditional' ? 'active' : ''}
        onClick={() => setMode('traditional')}
        aria-pressed={mode === 'traditional'}>
        传统
      </button>
    </div>
  );
};

export default ModeToggle;
