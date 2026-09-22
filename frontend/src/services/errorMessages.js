const translations = [
  [/^Username already registered$/i, '用户名已被注册'],
  [/^Incorrect username or password$/i, '用户名或密码错误'],
  [/^Password must be at least 6 characters$/i, '密码至少需要 6 个字符'],
  [/^New password must be at least 6 characters$/i, '新密码至少需要 6 个字符'],
  [/^Current password is incorrect$/i, '当前密码错误'],
  [/^User ['"](.+)['"] not found$/i, (_, name) => `未找到用户“${name}”`],
  [/^User not found$/i, '未找到用户'],
  [/^Image not found$/i, '未找到镜像'],
  [/^Container instance not found$/i, '未找到容器'],
  [/^Container is not running$/i, '容器未运行'],
  [/^Container is not stopped$/i, '容器未停止'],
  [/^Container is not in removed state$/i, '容器未处于已移除状态'],
  [/^You already have a running container/i, '你已有运行中的容器，请先停止它'],
  [/^GPU quota exceeded/i, 'GPU 配额不足'],
  [/^Not enough GPUs/i, '可用 GPU 数量不足'],
  [/^Not authorized/i, '没有执行此操作的权限'],
  [/^This endpoint requires LLM mode$/i, '此操作需要智能模式'],
  [/^Alert not found$/i, '未找到告警'],
  [/^Cannot delete yourself$/i, '不能删除自己的账号'],
  [/^Cannot delete the only admin account$/i, '不能删除唯一的管理员账号'],
  [/^Docker client not initialized$/i, 'Docker 服务不可用'],
  [/^Container not found in Docker$/i, 'Docker 中未找到该容器'],
  [/^Failed to stop Docker container/i, '停止 Docker 容器失败'],
  [/^Failed to remove Docker container/i, '删除 Docker 容器失败'],
  [/^Docker API error/i, 'Docker 操作失败'],
];

export const apiErrorText = (error, fallback = '操作失败，请稍后重试') => {
  const detail = error?.response?.data?.detail ?? error?.message;
  if (typeof detail !== 'string' || !detail.trim()) return fallback;
  if (/[\u3400-\u9fff]/.test(detail)) return detail;
  for (const [pattern, translation] of translations) {
    const match = detail.match(pattern);
    if (match) return typeof translation === 'function' ? translation(...match) : translation;
  }
  return fallback;
};
