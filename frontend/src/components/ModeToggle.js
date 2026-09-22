import React from 'react';
import { useMode } from '../context/ModeContext';

// 顶部模式开关：LLM（agent 助手）/ 传统（手动容器管理）。
const ModeToggle = ({ compact }) => {
  const { mode, setMode, modeError } = useMode();
  return (
    <>
      <div className={`mode-toggle${compact ? ' mode-toggle-compact' : ''}`}
        title="交互模式：智能模式使用助手管理容器；手动模式由用户自行操作">
        <button
          type="button"
          className={mode === 'llm' ? 'active' : ''}
          onClick={() => setMode('llm')}
          aria-pressed={mode === 'llm'}>
          智能模式
        </button>
        <button
          type="button"
          className={mode === 'traditional' ? 'active' : ''}
          onClick={() => setMode('traditional')}
          aria-pressed={mode === 'traditional'}>
          手动模式
        </button>
      </div>
      {modeError && <span className="mode-toggle-error">{modeError}</span>}
    </>
  );
};

export default ModeToggle;
