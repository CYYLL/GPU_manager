import axios from 'axios';

const api = axios.create({
  baseURL: process.env.REACT_APP_API_URL || '',
});

// 请求拦截器 - 添加认证头
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token');
    const tokenType = localStorage.getItem('tokenType') || 'bearer';
    
    if (token) {
      config.headers.Authorization = `${tokenType} ${token}`;
    }
    
    return config;
  },
  (error) => {
    return Promise.reject(error);
  }
);

// 响应拦截器 - 处理认证错误 + 幂等请求瞬时失败重试
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const cfg = error.config || {};

    if (error.response?.status === 401 && !window.location.pathname.startsWith('/login')) {
      // 非登录页面遇到 401，清除认证信息并重定向
      localStorage.removeItem('token');
      localStorage.removeItem('tokenType');
      localStorage.removeItem('user');
      window.location.href = '/login';
      return Promise.reject(error);
    }

    // 幂等 GET 在网络错误或 5xx（如 sqlite busy 的瞬时 500）时自动重试，
    // 避免页面因一次瞬时失败就永久空白/停在 loading。最多重试 2 次、逐次退避。
    const method = (cfg.method || '').toLowerCase();
    const status = error.response?.status;
    const transient = !error.response || status === 429 || (status >= 500 && status < 600);
    const attempts = cfg._retryCount || 0;
    if (cfg.retry !== false && method === 'get' && transient && attempts < 2) {
      cfg._retryCount = attempts + 1;
      await new Promise((resolve) => setTimeout(resolve, 400 * Math.pow(2, attempts)));
      return api.request(cfg);
    }
    return Promise.reject(error);
  }
);

export default api;