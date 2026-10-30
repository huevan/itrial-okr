/**
 * OKR 管理平台 —— 公共 JS 模块
 * 认证、API 封装、Toast、工具函数
 */

// ── Token 管理 ──────────────────────────────────────────────────────────

const OKR_TOKEN_KEY = 'okr_token';
const OKR_USER_KEY = 'okr_user';

function getToken() {
  return localStorage.getItem(OKR_TOKEN_KEY);
}

function getTokenPayload() {
  const token = getToken();
  if (!token) return null;
  try {
    const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(atob(part).split('').map(c =>
      '%' + c.charCodeAt(0).toString(16).padStart(2, '0')
    ).join(''));
    return JSON.parse(json);
  } catch { return null; }
}

function setToken(token) {
  localStorage.setItem(OKR_TOKEN_KEY, token);
}

function getUser() {
  try {
    return JSON.parse(localStorage.getItem(OKR_USER_KEY));
  } catch { return null; }
}

function setUser(user) {
  localStorage.setItem(OKR_USER_KEY, JSON.stringify(user));
}

function clearAuth() {
  localStorage.removeItem(OKR_TOKEN_KEY);
  localStorage.removeItem(OKR_USER_KEY);
}

function requireAuth() {
  if (!getToken()) {
    window.location.href = '/okr/login';
    return false;
  }
  const payload = getTokenPayload();
  if (payload && payload.role === 'superAdmin' && !payload.active_role) {
    window.location.href = '/okr/login?refresh_role=1';
    return false;
  }

  return true;
}

function logout() {
  clearAuth();
  window.location.href = '/okr/login';
}

async function switchOkrRole(role) {
  try {
    const result = await api('/api/okr/auth/role', {
      method: 'POST',
      body: { role },
    });
    if (!result) return;
    setToken(result.token);
    setUser(result.user);
    window.location.href = '/okr';
  } catch (e) {
    toast(e.message || '\u89d2\u8272\u5207\u6362\u5931\u8d25', 'error');
  }
}

function setupOkrRoleSwitcher(user, elementId) {
  const element = document.getElementById(elementId);
  const roles = user && user.available_roles;
  if (!element || !Array.isArray(roles) || roles.length < 2) return;

  const targetRole = user.role === 'dept_head' ? 'superAdmin' : 'dept_head';
  element.textContent = targetRole === 'superAdmin'
    ? '\u5207\u6362\u4e3a\u8d85\u7ea7\u7ba1\u7406\u5458'
    : (user.is_company_head ? '\u5207\u6362\u4e3a\u516c\u53f8\u8d1f\u8d23\u4eba' : '\u5207\u6362\u4e3a\u90e8\u95e8\u8d1f\u8d23\u4eba');
  element.style.display = '';
  element.onclick = () => switchOkrRole(targetRole);
}


// ── API 封装 ────────────────────────────────────────────────────────────

const _pendingRequests = new Set();

async function api(path, options = {}) {
  const { method = 'GET', body, noAuth = false } = options;

  // 写请求去重：相同 path+method 正在进行中则跳过
  const dedupeKey = method !== 'GET' ? `${method}:${path}` : null;
  if (dedupeKey) {
    if (_pendingRequests.has(dedupeKey)) return null;
    _pendingRequests.add(dedupeKey);
  }

  try {
    const headers = { 'Content-Type': 'application/json' };
    if (!noAuth) {
      const token = getToken();
      if (token) headers['Authorization'] = `Bearer ${token}`;
    }
    const fetchOpts = { method, headers };
    if (body) fetchOpts.body = JSON.stringify(body);

    const resp = await fetch(path, fetchOpts);

    if (resp.status === 401) {
      clearAuth();
      window.location.href = '/okr/login';
      throw new Error('未登录');
    }

    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.detail || data.msg || '请求失败');
    }
    return data;
  } finally {
    if (dedupeKey) _pendingRequests.delete(dedupeKey);
  }
}


// ── Toast 提示 ──────────────────────────────────────────────────────────

function _ensureToastContainer() {
  let c = document.querySelector('.toast-container');
  if (!c) {
    c = document.createElement('div');
    c.className = 'toast-container';
    document.body.appendChild(c);
  }
  return c;
}

function toast(msg, type = 'info', duration = 3000) {
  const container = _ensureToastContainer();
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(40px)';
    el.style.transition = '.3s';
    setTimeout(() => el.remove(), 300);
  }, duration);
}


// ── Modal 工具 ──────────────────────────────────────────────────────────

function showModal(id) {
  const mask = document.getElementById(id);
  if (mask) mask.classList.add('show');
}

function hideModal(id) {
  const mask = document.getElementById(id);
  if (mask) mask.classList.remove('show');
}


// ── 轻量确认弹窗 ────────────────────────────────────────────────────────

let _confirmDialogState = null;
let _confirmDialogPreviousFocus = null;

function _ensureConfirmDialog() {
  let layer = document.getElementById('okrConfirmLayer');
  if (layer) return layer;

  layer = document.createElement('div');
  layer.id = 'okrConfirmLayer';
  layer.className = 'confirm-layer';
  layer.setAttribute('role', 'presentation');
  layer.setAttribute('aria-hidden', 'true');
  layer.innerHTML = `
    <section class="confirm-dialog" role="dialog" aria-modal="true"
      aria-labelledby="okrConfirmTitle" aria-describedby="okrConfirmDescription">
      <div class="confirm-icon" aria-hidden="true">?</div>
      <div class="confirm-copy">
        <h3 id="okrConfirmTitle">请确认操作</h3>
        <p id="okrConfirmDescription"></p>
      </div>
      <div class="confirm-actions">
        <button type="button" class="btn btn-ghost" data-confirm-cancel>取消</button>
        <button type="button" class="btn btn-primary" data-confirm-submit>确认</button>
      </div>
    </section>`;

  layer.addEventListener('mousedown', event => {
    if (event.target === layer) _closeConfirmDialog(false);
  });
  layer.querySelector('[data-confirm-cancel]').addEventListener('click', () => _closeConfirmDialog(false));
  layer.querySelector('[data-confirm-submit]').addEventListener('click', () => _closeConfirmDialog(true));
  document.body.appendChild(layer);
  return layer;
}

function _handleConfirmDialogKeydown(event) {
  if (!_confirmDialogState) return;
  const layer = document.getElementById('okrConfirmLayer');
  if (!layer) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    _closeConfirmDialog(false);
    return;
  }
  if (event.key !== 'Tab') return;

  const cancelButton = layer.querySelector('[data-confirm-cancel]');
  const confirmButton = layer.querySelector('[data-confirm-submit]');
  const dialog = layer.querySelector('.confirm-dialog');
  if (event.shiftKey && (document.activeElement === cancelButton || !dialog.contains(document.activeElement))) {
    event.preventDefault();
    confirmButton.focus();
  } else if (!event.shiftKey && document.activeElement === confirmButton) {
    event.preventDefault();
    cancelButton.focus();
  }
}

function _closeConfirmDialog(confirmed) {
  const state = _confirmDialogState;
  if (!state) return;
  _confirmDialogState = null;

  const layer = document.getElementById('okrConfirmLayer');
  if (layer) {
    layer.classList.remove('show');
    layer.setAttribute('aria-hidden', 'true');
  }
  document.body.classList.remove('confirm-open');
  document.removeEventListener('keydown', _handleConfirmDialogKeydown);

  const previousFocus = _confirmDialogPreviousFocus;
  _confirmDialogPreviousFocus = null;
  if (previousFocus && typeof previousFocus.focus === 'function') previousFocus.focus();
  state.resolve(Boolean(confirmed));
}

function confirmAction(options = {}) {
  if (_confirmDialogState) _closeConfirmDialog(false);

  const layer = _ensureConfirmDialog();
  const dialog = layer.querySelector('.confirm-dialog');
  const icon = layer.querySelector('.confirm-icon');
  const title = layer.querySelector('#okrConfirmTitle');
  const description = layer.querySelector('#okrConfirmDescription');
  const cancelButton = layer.querySelector('[data-confirm-cancel]');
  const confirmButton = layer.querySelector('[data-confirm-submit]');
  const tone = ['danger', 'warning', 'primary'].includes(options.tone) ? options.tone : 'primary';

  dialog.className = `confirm-dialog confirm-${tone}`;
  icon.textContent = tone === 'danger' ? '!' : '?';
  title.textContent = options.title || '请确认操作';
  description.textContent = options.description || '';
  description.hidden = !description.textContent;
  cancelButton.textContent = options.cancelText || '取消';
  confirmButton.textContent = options.confirmText || '确认';
  confirmButton.className = `btn btn-${tone}`;

  _confirmDialogPreviousFocus = document.activeElement;
  layer.classList.add('show');
  layer.setAttribute('aria-hidden', 'false');
  document.body.classList.add('confirm-open');
  document.addEventListener('keydown', _handleConfirmDialogKeydown);

  return new Promise(resolve => {
    _confirmDialogState = { resolve };
    requestAnimationFrame(() => confirmButton.focus());
  });
}


// ── Tab 切换 ────────────────────────────────────────────────────────────

function initTabs(tabGroupId) {
  const group = document.getElementById(tabGroupId);
  if (!group) return;
  const buttons = group.querySelectorAll('.tab-btn');
  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.tab;
      // 切换按钮状态
      buttons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      // 切换面板
      const panels = group.parentElement.querySelectorAll('.tab-panel');
      panels.forEach(p => {
        p.classList.toggle('active', p.id === target);
      });
      // 触发自定义事件
      group.dispatchEvent(new CustomEvent('tabChange', { detail: { tab: target } }));
    });
  });
}


// ── 状态徽标 ────────────────────────────────────────────────────────────

const STATUS_MAP = {
  draft:    { label: '草稿', cls: 'badge-gray' },
  pending:  { label: '待审核', cls: 'badge-warning' },
  approved: { label: '已通过', cls: 'badge-success' },
  rejected: { label: '已驳回', cls: 'badge-danger' },
  active:   { label: '进行中', cls: 'badge-info' },
  completed:{ label: '已完成', cls: 'badge-success' },
};

function statusBadge(status) {
  const s = STATUS_MAP[status] || { label: status, cls: 'badge-gray' };
  return `<span class="badge ${s.cls}">${s.label}</span>`;
}

const ROLE_MAP = {
  superAdmin: '超级管理员',
  admin: '管理员',
  dept_head: '部门负责人',
  user: '普通用户',
};

function roleLabel(role) {
  return ROLE_MAP[role] || role;
}

function roleLabelForUser(user) {
  if (user && user.role === 'dept_head' && user.is_company_head) return '公司负责人';
  return roleLabel(user ? user.role : '');
}


// ── 进度条 HTML ─────────────────────────────────────────────────────────

function progressBarHTML(progress) {
  const p = Math.round(progress || 0);
  return `<div class="flex" style="align-items:center;gap:8px;">
    <div class="progress-bar" style="flex:1"><div class="fill" style="width:${p}%"></div></div>
    <span style="font-size:12px;color:var(--text-secondary);min-width:36px">${p}%</span>
  </div>`;
}


// ── 格式化时间 ──────────────────────────────────────────────────────────

function formatTime(t) {
  if (!t) return '-';
  return t.replace('T', ' ').slice(0, 16);
}
