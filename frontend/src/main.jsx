import React, { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App.jsx';
import './styles.css';

class AppErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('前端渲染失败', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return <main style={{ padding: '32px', color: '#102a2e', fontFamily: 'Noto Sans SC, Microsoft YaHei, sans-serif' }}>
      <h1>页面加载失败</h1>
      <p>请把下面的错误信息发给开发人员：</p>
      <pre style={{ whiteSpace: 'pre-wrap', color: '#a94232' }}>{this.state.error.stack || this.state.error.message}</pre>
    </main>;
  }
}

createRoot(document.getElementById('root')).render(
  <StrictMode><AppErrorBoundary><App /></AppErrorBoundary></StrictMode>,
);
