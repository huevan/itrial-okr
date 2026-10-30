/**
 * OKR 脑图可视化组件 (D3.js v7)
 * 支持公司看板和部门看板的树形展示，支持点击展开/折叠
 */

const OKRMindmap = {
  _treeControllers: new Map(),

  /**
   * 渲染公司看板脑图
   */
  renderCompany(containerId, data, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    this._treeControllers.delete(containerId);
    container.innerHTML = '';

    // 整理部门信息 map 和所有部门 OKR 列表
    const deptInfoMap = {};
    const allDeptOkrs = [];
    (data.departments || []).forEach(d => {
      deptInfoMap[d.department.id] = d.department;
      (d.okrs || []).forEach(okr => allDeptOkrs.push(okr));
    });

    const hasAnything = (data.company_okrs && data.company_okrs.length > 0) || allDeptOkrs.length > 0;
    if (!hasAnything) {
      container.innerHTML = '<div style="text-align:center;padding:60px;color:#94a3b8;">暂无公司 OKR，管理员可创建</div>';
      return;
    }

    const _trim = (s, n) => s && s.length > n ? s.slice(0, n) + '...' : (s || '');

    // 将公司 OKR id 收集为 Set
    const companyOkrIds = new Set((data.company_okrs || []).map(o => o.id));

    // 按对齐关系分组：alignedMap[company_okr_id] = [dept_okr, ...]
    const alignedMap = {};
    const unalignedDeptOkrs = [];
    allDeptOkrs.forEach(okr => {
      if (okr.aligned_objective_id && companyOkrIds.has(okr.aligned_objective_id)) {
        if (!alignedMap[okr.aligned_objective_id]) alignedMap[okr.aligned_objective_id] = [];
        alignedMap[okr.aligned_objective_id].push(okr);
      } else {
        unalignedDeptOkrs.push(okr);
      }
    });

    // 构建部门 OKR 节点
    const deptOkrNode = (okr, showDeptName = true) => ({
      name: _trim(okr.title, 18),
      fullName: okr.title,
      subtitle: showDeptName
        ? ((deptInfoMap[okr.department_id] || {}).name || '')
        : (okr.owner_name || ''),
      progress: okr.progress,
      owner: okr.owner_name || '',
      status: okr.status,
      id: okr.id,
      type: 'department',
      weight: okr.weight || 1,
      krs: okr.key_results || [],
      hasOkr: true,
      children: [],
    });

    // 公司 OKR 节点
    const companyChildren = (data.company_okrs || []).map(okr => ({
      name: okr.title,
      fullName: okr.title,
      progress: okr.progress,
      owner: okr.owner_name,
      status: okr.status,
      id: okr.id,
      type: 'company',
      weight: okr.weight || 1,
      krs: okr.key_results || [],
      children: (alignedMap[okr.id] || []).map(o => deptOkrNode(o, true)),
    }));

    // 未对齐的部门 OKR 按部门分组
    if (unalignedDeptOkrs.length > 0) {
      const deptGroupMap = {};
      const deptGroupOrder = [];
      unalignedDeptOkrs.forEach(okr => {
        const did = okr.department_id;
        if (!deptGroupMap[did]) { deptGroupMap[did] = []; deptGroupOrder.push(did); }
        deptGroupMap[did].push(okr);
      });

      const deptGroupNodes = deptGroupOrder.map(did => {
        const okrs = deptGroupMap[did];
        const dept = deptInfoMap[did] || {};
        const avgProgress = Math.round(okrs.reduce((s, o) => s + (o.progress || 0), 0) / okrs.length);
        return {
          name: dept.name || `部门${did}`,
          subtitle: `${okrs.length} 个OKR`,
          progress: avgProgress,
          owner: dept.head_name || '',
          status: 'active',
          type: 'dept-group',
          children: [],
          _children: okrs.map(o => deptOkrNode(o, false)),
          _okrCount: okrs.length,
        };
      });

      companyChildren.push({
        name: '其他目标',
        subtitle: `${deptGroupOrder.length} 个部门`,
        progress: 0,
        status: 'empty',
        type: 'placeholder',
        children: deptGroupNodes,
      });
    }

    const root = {
      name: data.company_name || '公司 OKR',
      type: 'root',
      progress: 0,
      children: companyChildren,
    };

    const tree = root.children.length === 1 ? root.children[0] : root;
    this._renderTree(container, tree, options);
  },

  /**
   * 渲染部门看板脑图
   */
  renderDepartment(containerId, data, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;
    this._treeControllers.delete(containerId);
    container.innerHTML = '';

    const deptOkrs = Array.isArray(data.dept_okrs) ? data.dept_okrs : [];
    const members = Array.isArray(data.members) ? data.members : [];
    const allPersonalOkrs = [];
    const memberMap = {};
    members.forEach(m => {
      memberMap[m.user.id] = m.user;
      if (m.okrs) m.okrs.forEach(okr => allPersonalOkrs.push(okr));
    });

    if (deptOkrs.length === 0 && allPersonalOkrs.length === 0 && members.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:60px;color:#94a3b8;">本月暂无公司月度 OKR 或个人 OKR</div>';
      return;
    }

    // 按对齐关系分组
    const deptOkrIds = new Set(deptOkrs.map(o => o.id));
    const alignedByDeptOkr = {};
    const unalignedOkrs = [];
    allPersonalOkrs.forEach(okr => {
      if (okr.aligned_objective_id && deptOkrIds.has(okr.aligned_objective_id)) {
        if (!alignedByDeptOkr[okr.aligned_objective_id]) alignedByDeptOkr[okr.aligned_objective_id] = [];
        alignedByDeptOkr[okr.aligned_objective_id].push(okr);
      } else {
        unalignedOkrs.push(okr);
      }
    });

    const _trim = (s, n) => s && s.length > n ? s.slice(0, n) + '...' : (s || '');
    const groupByUser = (okrs) => {
      const map = {};
      okrs.forEach(okr => {
        const uid = okr.owner_id;
        if (!map[uid]) map[uid] = { user: memberMap[uid], okrs: [] };
        map[uid].okrs.push(okr);
      });
      return Object.values(map).map(g => {
        const avgProgress = Math.round(g.okrs.reduce((s, o) => s + (o.progress || 0), 0) / g.okrs.length);
        const okrChildren = g.okrs.map((okr, objectiveIndex) => {
          const krChildren = (okr.key_results || []).map((kr, krIndex) => {
            const target = Number(kr.target_value) || 0;
            const current = Number(kr.current_value) || 0;
            const calculatedProgress = target > 0 ? current / target * 100 : 0;
            return {
              name: `KR${krIndex + 1} · ${_trim(kr.title, 14)}`,
              fullName: kr.title,
              subtitle: `${current} / ${target} ${kr.unit || ''}`.trim(),
              progress: kr.progress == null ? calculatedProgress : kr.progress,
              type: 'kr-detail',
              children: [],
            };
          });
          return {
            name: `O${objectiveIndex + 1} · ${_trim(okr.title, 12)}`,
            fullName: okr.title,
            subtitle: okr.description || `个人目标 O${objectiveIndex + 1}`,
            progress: okr.progress,
            status: okr.status,
            id: okr.id,
            ownerId: okr.owner_id,
            type: 'okr-detail',
            krs: okr.key_results || [],
            rejectionReason: okr.status === 'rejected' ? (okr.latest_review?.comment || '') : '',
            encouragementCount: Number(okr.encouragement_count) || 0,
            encouragedByMe: !!okr.encouraged_by_me,
            children: krChildren,
          };
        });
        const hasNonActive = g.okrs.some(o => !['active', 'completed'].includes(o.status));
        const worstStatus = g.okrs.find(o => o.status === 'pending')?.status
          || g.okrs.find(o => o.status === 'draft')?.status
          || g.okrs.find(o => o.status === 'rejected')?.status
          || 'active';
        return {
          name: g.user.name,
          subtitle: g.user.department_name || '未分配部门',
          progress: avgProgress,
          status: worstStatus,
          _hasNonActive: hasNonActive,
          type: 'personal',
          aligned: !!g.okrs[0].aligned_objective_id,
          hasOkr: true,
          userId: g.user.id,
          note: g.user.note || '',
          canEditNote: !!g.user.can_edit_note,
          children: [],
          _children: okrChildren,
          _okrCount: g.okrs.length,
        };
      });
    };

    // 构建部门 OKR 子节点
    const deptChildren = deptOkrs.map(okr => ({
      name: okr.title,
      fullName: okr.title,
      progress: okr.progress,
      owner: okr.owner_name || '',
      status: okr.status,
      id: okr.id,
      type: 'department',
      weight: okr.weight || 1,
      krs: okr.key_results || [],
      children: groupByUser(alignedByDeptOkr[okr.id] || []),
    }));

    // 未对齐 + 未创建 OKR 的成员
    const unalignedUserNodes = groupByUser(unalignedOkrs);
    const noOkrMembers = members
      .filter(m => !m.has_okr || m.okrs.length === 0)
      .map(m => ({
        name: m.user.name, subtitle: `${m.user.department_name || '未分配部门'} · 未创建 OKR`, progress: 0,
        status: 'empty', type: 'personal', hasOkr: false, userId: m.user.id,
        note: m.user.note || '', canEditNote: !!m.user.can_edit_note, children: [],
      }));

    const otherNodes = [...unalignedUserNodes, ...noOkrMembers];
    if (otherNodes.length > 0) {
      const uniqueCount = new Set([
        ...unalignedUserNodes.map(n => n.userId || n.name),
        ...noOkrMembers.map(n => n.userId || n.name),
      ]).size;
      deptChildren.push({
        name: '其他目标',
        subtitle: `${uniqueCount} 人`,
        progress: 0, status: 'empty', type: 'placeholder',
        children: otherNodes,
      });
    }

    const root = {
      name: data.department.name,
      owner: data.department.head_name || '',
      type: 'department',
      progress: deptOkrs.length
        ? Math.round(deptOkrs.reduce((s, o) => s + (o.progress || 0), 0) / deptOkrs.length)
        : 0,
      children: deptChildren,
    };
    this._renderTree(container, root, options);
  },

  expandAllDetails(containerId) {
    const controller = this._treeControllers.get(containerId);
    if (!controller) return false;
    const expand = node => {
      if (node._children && node._children.length > 0) {
        node.children = node._children;
        node._children = [];
      }
      (node.children || []).forEach(expand);
    };
    expand(controller.rootData);
    controller.update();
    return true;
  },

  collapseAllDetails(containerId) {
    const controller = this._treeControllers.get(containerId);
    if (!controller) return false;
    const collapse = node => {
      if (node.type === 'personal' && node.children && node.children.length > 0) {
        node._children = node.children;
        node.children = [];
        return;
      }
      (node.children || []).forEach(collapse);
    };
    collapse(controller.rootData);
    controller.update();
    return true;
  },

  hasTree(containerId) {
    return this._treeControllers.has(containerId);
  },

  _findObjectiveNode(containerId, objectiveId) {
    const controller = this._treeControllers.get(containerId);
    if (!controller) return null;
    const targetId = Number(objectiveId);
    let objective = null;
    const visit = node => {
      if (!node || objective) return;
      if (node.type === 'okr-detail' && Number(node.id) === targetId) {
        objective = node;
        return;
      }
      (node.children || []).forEach(visit);
      (node._children || []).forEach(visit);
    };
    visit(controller.rootData);
    return objective ? { controller, objective } : null;
  },

  getObjectiveEncouragement(containerId, objectiveId) {
    const match = this._findObjectiveNode(containerId, objectiveId);
    if (!match) return null;
    const { objective } = match;
    return {
      encouraged: !!objective.encouragedByMe,
      count: Number(objective.encouragementCount) || 0,
      pending: !!objective.encouragementPending,
    };
  },

  updateObjectiveEncouragement(containerId, objectiveId, nextState) {
    const match = this._findObjectiveNode(containerId, objectiveId);
    if (!match) return false;
    const { controller, objective } = match;
    const targetId = Number(objectiveId);
    objective.encouragedByMe = !!nextState.encouraged;
    objective.encouragementCount = Math.max(0, Number(nextState.count) || 0);
    objective.encouragementPending = !!nextState.pending;

    const canToggle = controller.options.onToggleEncouragement
      && objective.ownerId !== controller.options.currentUserId;
    const control = d3.select(document.getElementById(containerId))
      .selectAll('g.encouragement-control')
      .filter(d => d?.data && Number(d.data.id) === targetId);
    control
      .attr('aria-label', `${objective.encouragedByMe ? '取消鼓励' : '点赞鼓励'}，当前 ${objective.encouragementCount} 次`)
      .attr('aria-busy', objective.encouragementPending ? 'true' : null)
      .attr('aria-disabled', objective.encouragementPending ? 'true' : null)
      .style('cursor', objective.encouragementPending ? 'wait' : (canToggle ? 'pointer' : 'default'))
      .style('opacity', objective.encouragementPending ? 0.65 : 1);
    control.select('rect')
      .attr('fill', objective.encouragedByMe ? '#fce7f3' : '#fff');
    control.select('text')
      .text(`${objective.encouragedByMe ? '♥' : '♡'} ${objective.encouragementCount}`);
    control.select('title')
      .text(objective.ownerId === controller.options.currentUserId
        ? '收到的鼓励数'
        : (objective.encouragedByMe ? '取消鼓励' : '点赞鼓励'));
    return true;
  },

  /**
   * 内部：使用 D3 渲染可交互树形脑图（支持展开/折叠）
   * options.onEditDept(id) — 点击部门OKR节点右上角编辑图标时回调
   * options.onEditCompany(id) — 点击公司OKR节点右上角编辑图标时回调
   */
  _renderTree(container, rootData, options = {}) {
    const containerWidth = container.clientWidth || 900;
    const margin = { top: 30, right: 160, bottom: 30, left: 80 };
    const innerW = Math.max(containerWidth, 600) - margin.left - margin.right;

    // 层级尺寸：根节点 > 一级 > 二级+
    const sizes = [
      { w: 160, h: 62, rx: 14, fontSize: 13, subSize: 11, maxChars: 9 },  // depth 0
      { w: 140, h: 56, rx: 12, fontSize: 12, subSize: 10, maxChars: 8 },  // depth 1
      { w: 124, h: 48, rx: 10, fontSize: 11, subSize: 9, maxChars: 7 },   // depth 2+
    ];
    const detailW = 180, detailH = 72;
    const employeeSize = { w: 176, h: 64, rx: 12, fontSize: 12, subSize: 10, maxChars: 10 };

    const getSize = d => {
      if (d.data.type === 'okr-detail') return { w: detailW, h: detailH, rx: 10, fontSize: 11, subSize: 9, maxChars: 14 };
      if (d.data.type === 'kr-detail') return { w: 200, h: 64, rx: 10, fontSize: 10, subSize: 9, maxChars: 16 };
      if (d.data.type === 'personal') return employeeSize;
      return sizes[Math.min(d.depth, 2)];
    };

    const _trim = (s, n) => s && s.length > n ? s.slice(0, n) + '...' : (s || '');
    const _progressValue = value => Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
    const _escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));

    const root = d3.hierarchy(rootData);
    root.each(d => {
      if (d.data._children && d.data._children.length > 0) {
        d._collapsed = true;
      }
    });

    const self = this;

    // ── 创建 Tooltip 容器 ──
    container.style.position = 'relative';
    const tooltip = document.createElement('div');
    tooltip.className = 'okr-mindmap-tooltip';
    tooltip.style.cssText = `
      position:absolute; z-index:100; pointer-events:none;
      background:#fff; border:1px solid #e2e8f0; border-radius:10px;
      box-shadow:0 8px 24px rgba(0,0,0,0.12); padding:14px 16px;
      max-width:340px; font-size:13px; color:#334155; line-height:1.6;
      opacity:0; transition:opacity 0.15s;
    `;
    container.appendChild(tooltip);

    function showTooltip(event, d) {
      const data = d.data;
      if (data.type === 'placeholder' || data.type === 'root') { tooltip.style.opacity = 0; return; }

      const statusMap = {
        draft: { label: '草稿', color: '#64748b', bg: '#f1f5f9' },
        pending: { label: '待审核', color: '#d97706', bg: '#fef3c7' },
        approved: { label: '已批准', color: '#059669', bg: '#d1fae5' },
        rejected: { label: '已驳回', color: '#dc2626', bg: '#fef2f2' },
        active: { label: '进行中', color: '#2563eb', bg: '#dbeafe' },
        completed: { label: '已完成', color: '#059669', bg: '#d1fae5' },
        empty: { label: '未提交', color: '#64748b', bg: '#f1f5f9' },
      };
      const st = statusMap[data.status] || statusMap.active;
      const progress = Math.round(data.progress || 0);
      const progressColor = progress >= 70 ? '#10b981' : progress >= 40 ? '#f59e0b' : '#ef4444';

      let html = '';

      // 标题
      html += `<div style="font-weight:700;font-size:14px;color:#0f172a;margin-bottom:6px">${_escapeHtml(data.fullName || data.name)}</div>`;
      if (data.type === 'okr-detail' && data.subtitle) {
        html += `<div style="font-size:12px;color:#64748b;margin-bottom:8px">${_escapeHtml(data.subtitle)}</div>`;
      }

      // 元信息行
      const meta = [];
      if (data.owner) meta.push(`<span style="color:#64748b">负责人</span> ${_escapeHtml(data.owner)}`);
      if (data.subtitle && !['dept-group', 'okr-detail', 'kr-detail'].includes(data.type))
        meta.push(`<span style="color:#64748b">部门</span> ${_escapeHtml(data.subtitle)}`);
      if (data.weight && data.weight !== 1)
        meta.push(`<span style="color:#64748b">权重</span> <b>${data.weight}</b>`);
      if (meta.length) html += `<div style="font-size:12px;margin-bottom:8px">${meta.join(' &nbsp;|&nbsp; ')}</div>`;

      // 状态 + 进度条
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">`;
      html += `<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;color:${st.color};background:${st.bg}">${st.label}</span>`;
      html += `<div style="flex:1;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden">`;
      html += `<div style="width:${progress}%;height:100%;background:${progressColor};border-radius:3px"></div></div>`;
      html += `<span style="font-size:12px;font-weight:700;color:${progressColor}">${progress}%</span>`;
      html += `</div>`;

      if (data.note) {
        html += `<div style="border-top:1px solid #f1f5f9;padding-top:8px;margin-top:4px">`;
        html += `<div style="font-size:11px;color:#8b5cf6;font-weight:700;margin-bottom:3px">管理备注</div>`;
        html += `<div style="font-size:12px;color:#475569;white-space:pre-wrap">${_escapeHtml(data.note)}</div></div>`;
      }

      if (data.status === 'rejected' && data.rejectionReason) {
        html += `<div style="border:1px solid #fecaca;background:#fef2f2;border-radius:7px;padding:7px 9px;margin-top:8px">`;
        html += `<div style="font-size:11px;color:#dc2626;font-weight:700">驳回原因</div>`;
        html += `<div style="font-size:12px;color:#7f1d1d;white-space:pre-wrap">${_escapeHtml(data.rejectionReason)}</div></div>`;
      }

      if (data.type === 'okr-detail') {
        const encouragementText = data.encouragedByMe ? '，你已鼓励' : '';
        html += `<div style="font-size:11px;color:#db2777;margin-top:7px">♥ 收到 ${data.encouragementCount || 0} 次鼓励${encouragementText}</div>`;
      }

      // KR 列表
      const krs = data.krs || [];
      if (krs.length > 0) {
        html += `<div style="border-top:1px solid #f1f5f9;padding-top:8px;margin-top:4px">`;
        html += `<div style="font-size:11px;color:#94a3b8;font-weight:600;margin-bottom:6px">关键结果 (${krs.length})</div>`;
        krs.forEach((kr, i) => {
          const krProgress = kr.target_value > 0 ? Math.round((kr.current_value / kr.target_value) * 100) : 0;
          const krColor = krProgress >= 70 ? '#10b981' : krProgress >= 40 ? '#f59e0b' : '#ef4444';
          html += `<div style="margin-bottom:${i < krs.length - 1 ? '6' : '0'}px">`;
          html += `<div style="display:flex;align-items:baseline;gap:4px;font-size:12px">`;
          html += `<span style="color:#94a3b8;flex-shrink:0">KR${i + 1}</span>`;
          html += `<span style="color:#334155;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_escapeHtml(kr.title)}</span>`;
          html += `<span style="flex-shrink:0;font-weight:600;color:${krColor}">${_escapeHtml(kr.current_value)}/${_escapeHtml(kr.target_value)}${_escapeHtml(kr.unit)}</span>`;
          html += `</div>`;
          html += `<div style="height:3px;background:#f1f5f9;border-radius:2px;margin-top:3px;overflow:hidden">`;
          html += `<div style="width:${Math.min(krProgress, 100)}%;height:100%;background:${krColor};border-radius:2px"></div></div>`;
          html += `</div>`;
        });
        html += `</div>`;
      }

      // 对齐 / 折叠提示
      if (data.aligned) html += `<div style="font-size:11px;color:#8b5cf6;margin-top:6px">🔗 已对齐上级目标</div>`;
      if (data._okrCount) html += `<div style="font-size:11px;color:#64748b;margin-top:4px">📂 ${data._okrCount} 个 O（点击 + 展开 O 和 KR）</div>`;

      tooltip.innerHTML = html;
      tooltip.style.opacity = '1';

      // 定位：先渲染获取实际尺寸，再做边界修正
      const containerRect = container.getBoundingClientRect();
      const tipW = tooltip.offsetWidth;
      const tipH = tooltip.offsetHeight;
      const cW = container.scrollWidth;
      const cH = container.scrollHeight;
      const mx = event.clientX - containerRect.left + container.scrollLeft;
      const my = event.clientY - containerRect.top + container.scrollTop;

      // 优先显示在鼠标右下方
      let left = mx + 16;
      let top = my - 10;

      // 右侧溢出 → 显示在鼠标左侧
      if (left + tipW + 8 > cW) left = mx - tipW - 16;
      // 仍然溢出左侧 → 贴左边
      if (left < 4) left = 4;

      // 底部溢出 → 上移
      if (top + tipH + 8 > cH) top = cH - tipH - 8;
      // 顶部溢出 → 贴顶
      if (top < 4) top = 4;

      tooltip.style.left = left + 'px';
      tooltip.style.top = top + 'px';
    }

    function hideTooltip() {
      tooltip.style.opacity = '0';
    }

    function update() {
      const freshRoot = d3.hierarchy(rootData);

      const layoutW = Math.max(innerW, freshRoot.height * 220);
      const treeLayout = d3.tree().nodeSize([90, layoutW / (freshRoot.height || 1)]);
      treeLayout(freshRoot);

      let minX = Infinity, maxX = -Infinity;
      freshRoot.each(d => { minX = Math.min(minX, d.x); maxX = Math.max(maxX, d.x); });
      const treeH = maxX - minX;
      const height = Math.max(400, treeH + margin.top + margin.bottom + 80);
      const offsetY = -minX + margin.top + 40;

      // 清空重绘（保留 tooltip）
      const existingSvg = container.querySelector('svg');
      if (existingSvg) existingSvg.remove();

      const canvasW = margin.left + layoutW + margin.right;
      const svg = d3.select(container)
        .append('svg')
        .attr('width', Math.max(containerWidth, canvasW))
        .attr('height', height)
        .attr('viewBox', `0 0 ${canvasW} ${height}`)
        .attr('preserveAspectRatio', 'xMidYMid meet')
        .append('g')
        .attr('transform', `translate(${margin.left}, ${offsetY})`);

      // ── 连线 ──
      svg.selectAll('.link')
        .data(freshRoot.links())
        .enter()
        .append('path')
        .attr('class', 'link')
        .attr('d', d => `M${d.source.y},${d.source.x}C${(d.source.y + d.target.y) / 2},${d.source.x} ${(d.source.y + d.target.y) / 2},${d.target.x} ${d.target.y},${d.target.x}`)
        .attr('fill', 'none')
        .attr('stroke', d => {
          if (d.target.data.type === 'kr-detail') return '#7dd3fc';
          if (d.target.data.type === 'okr-detail') return '#c4b5fd';
          if (d.target.data.aligned) return '#8b5cf6';
          if (d.target.data.type === 'placeholder') return '#cbd5e1';
          return '#a5b4fc';
        })
        .attr('stroke-width', d => {
          if (d.target.data.type === 'kr-detail') return 1;
          if (d.target.data.type === 'okr-detail') return 1;
          if (d.target.data.aligned) return 2.5;
          // 按深度递减
          return Math.max(1, 2 - d.target.depth * 0.3);
        })
        .attr('stroke-dasharray', d => {
          if (d.target.data.type === 'placeholder') return '6,4';
          if (d.target.data.aligned) return '8,4';
          return 'none';
        });

      // ── 节点组 ──
      const nodes = svg.selectAll('.node')
        .data(freshRoot.descendants())
        .enter()
        .append('g')
        .attr('class', 'node')
        .attr('transform', d => `translate(${d.y},${d.x})`)
        .on('mouseenter', function(event, d) { showTooltip(event, d); })
        .on('mousemove', function(event, d) { showTooltip(event, d); })
        .on('mouseleave', hideTooltip);

      const isDetail = d => ['okr-detail', 'kr-detail'].includes(d.data.type);
      const isCollapsible = d =>
        (d.data.type === 'personal' && d.data.hasOkr) ||
        (d.data.type === 'dept-group');

      // ── 节点背景矩形（按层级尺寸） ──
      nodes.append('rect')
        .attr('x', d => -getSize(d).w / 2)
        .attr('y', d => -getSize(d).h / 2)
        .attr('width', d => getSize(d).w)
        .attr('height', d => getSize(d).h)
        .attr('rx', d => getSize(d).rx)
        .attr('fill', d => {
          if (d.data.type === 'kr-detail') return '#f0f9ff';
          if (d.data.type === 'okr-detail') return '#faf5ff';
          if (d.data.type === 'company') return '#6d28d9';
          if (d.data.type === 'department' || d.data.type === 'dept-group') return '#3b82f6';
          if (d.data.type === 'placeholder') return '#f1f5f9';
          if (d.data.type === 'personal') return d.data.aligned ? '#fbfaff' : '#fff';
          if (d.data.aligned) return '#ede9fe';
          if (d.data.type === 'root') return '#1e1b4b';
          return '#fff';
        })
        .attr('stroke', d => {
          if (d.data.type === 'kr-detail') return '#7dd3fc';
          if (d.data.type === 'okr-detail') return '#c4b5fd';
          if (d.data.type === 'placeholder') return '#cbd5e1';
          if (d.data.type === 'company') return '#5b21b6';
          if (d.data.type === 'department' || d.data.type === 'dept-group') return '#2563eb';
          if (d.data.type === 'personal') return d.data.aligned ? '#a78bfa' : '#d8dee9';
          if (d.data.aligned) return '#8b5cf6';
          if (d.data.type === 'root') return '#312e81';
          return '#e2e8f0';
        })
        .attr('stroke-width', d => d.data.type === 'personal'
          ? 1.15
          : ((d.data.aligned || ['okr-detail', 'kr-detail'].includes(d.data.type)) ? 2 : 1))
        .attr('stroke-dasharray', d => d.data.type === 'placeholder' ? '4,3' : 'none')
        .style('cursor', d => isCollapsible(d) ? 'pointer' : 'default')
        .style('filter', d => {
          if (d.data.type === 'placeholder') return 'none';
          if (d.data.type === 'personal') return 'drop-shadow(0 3px 8px rgba(76,29,149,0.08))';
          return 'drop-shadow(0 2px 4px rgba(0,0,0,0.08))';
        })
        .style('opacity', d => {
          if (d.data.type === 'placeholder') return 0.85;
          return Math.max(0.88, 1 - d.depth * 0.04);
        });

      // ── OKR 详情节点（展开后显示）：标题 + 描述 + 进度 ──
      const detailNodes = nodes.filter(d => isDetail(d));

      detailNodes.append('text')
        .attr('text-anchor', 'middle').attr('y', -18)
        .attr('font-size', '11px').attr('font-weight', '600')
        .attr('fill', d => d.data.type === 'kr-detail' ? '#0369a1' : '#6d28d9')
        .text(d => _trim(d.data.name, getSize(d).maxChars));

      detailNodes.append('text')
        .attr('text-anchor', 'middle').attr('y', 0)
        .attr('font-size', '9px')
        .attr('fill', '#64748b')
        .text(d => _trim(d.data.subtitle, 18));

      const statusLabel = { draft: '草稿', pending: '待审核', rejected: '已驳回' };
      const statusColor = { draft: '#94a3b8', pending: '#f59e0b', rejected: '#ef4444' };

      detailNodes.filter(d => statusLabel[d.data.status]).append('text')
        .attr('text-anchor', 'middle').attr('y', 18)
        .attr('font-size', '10px').attr('font-weight', '600')
        .attr('fill', d => statusColor[d.data.status])
        .text(d => statusLabel[d.data.status]);

      detailNodes.filter(d => !statusLabel[d.data.status]).each(function (d) {
        const progress = _progressValue(d.data.progress);
        const isKr = d.data.type === 'kr-detail';
        const progressColor = isKr ? '#0ea5e9' : '#10b981';
        const progressBg = isKr ? '#e0f2fe' : '#d1fae5';
        const progressWidth = 82;
        const progressG = d3.select(this).append('g')
          .attr('class', 'detail-progress')
          .attr('transform', 'translate(0, 18)');

        progressG.append('title').text(`进度 ${progress}%`);
        progressG.append('rect')
          .attr('x', -56).attr('y', -3.5)
          .attr('width', progressWidth).attr('height', 7)
          .attr('rx', 3.5)
          .attr('fill', progressBg);
        progressG.append('rect')
          .attr('x', -56).attr('y', -3.5)
          .attr('width', progressWidth * progress / 100).attr('height', 7)
          .attr('rx', 3.5)
          .attr('fill', progressColor);
        progressG.append('text')
          .attr('x', 34).attr('y', 0)
          .attr('dominant-baseline', 'central')
          .attr('font-size', '10px').attr('font-weight', '700')
          .attr('fill', progressColor)
          .text(`${progress}%`);
        });

      // ── Object 鼓励数量；其他人可点击点赞/取消鼓励 ──
      const encouragementNodes = nodes.filter(d => d.data.type === 'okr-detail' && d.data.id);
      const encouragementG = encouragementNodes.append('g')
        .attr('class', 'encouragement-control')
        .attr('transform', d => `translate(${getSize(d).w / 2 - 28}, ${-getSize(d).h / 2 + 10})`)
        .attr('role', d => options.onToggleEncouragement && d.data.ownerId !== options.currentUserId ? 'button' : null)
        .attr('tabindex', d => options.onToggleEncouragement && d.data.ownerId !== options.currentUserId ? 0 : null)
        .attr('aria-label', d => `${d.data.encouragedByMe ? '取消鼓励' : '点赞鼓励'}，当前 ${d.data.encouragementCount || 0} 次`)
        .style('cursor', d => options.onToggleEncouragement && d.data.ownerId !== options.currentUserId ? 'pointer' : 'default')
        .on('click', function(event, d) {
          event.stopPropagation();
          if (!d.data.encouragementPending
              && options.onToggleEncouragement
              && d.data.ownerId !== options.currentUserId) {
            options.onToggleEncouragement(d.data.id);
          }
        })
        .on('keydown', function(event, d) {
          if (!['Enter', ' '].includes(event.key)) return;
          event.preventDefault();
          event.stopPropagation();
          if (!d.data.encouragementPending
              && options.onToggleEncouragement
              && d.data.ownerId !== options.currentUserId) {
            options.onToggleEncouragement(d.data.id);
          }
        });
      encouragementG.append('rect')
        .attr('x', -20).attr('y', -8).attr('width', 40).attr('height', 16).attr('rx', 8)
        .attr('fill', d => d.data.encouragedByMe ? '#fce7f3' : '#fff')
        .attr('stroke', '#f9a8d4');
      encouragementG.append('text')
        .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
        .attr('font-size', '9px').attr('font-weight', '700').attr('fill', '#db2777')
        .text(d => `${d.data.encouragedByMe ? '♥' : '♡'} ${d.data.encouragementCount || 0}`);
      encouragementG.append('title')
        .text(d => d.data.ownerId === options.currentUserId ? '收到的鼓励数' : (d.data.encouragedByMe ? '取消鼓励' : '点赞鼓励'));

      // ── 普通节点文字（按层级字号） ──
      const normalNodes = nodes.filter(d => !isDetail(d));
      const employeeNodes = normalNodes.filter(d => d.data.type === 'personal');
      const otherNormalNodes = normalNodes.filter(d => d.data.type !== 'personal');

      employeeNodes.append('rect')
        .attr('class', 'employee-avatar-zone')
        .attr('x', d => -getSize(d).w / 2 + 8).attr('y', d => -getSize(d).h / 2 + 8)
        .attr('width', 44).attr('height', d => getSize(d).h - 16).attr('rx', 10)
        .attr('fill', d => d.data.aligned ? '#f1edff' : '#f8fafc');

      employeeNodes.append('circle')
        .attr('class', 'employee-avatar')
        .attr('cx', d => -getSize(d).w / 2 + 30).attr('cy', 0).attr('r', 16)
        .attr('fill', d => d.data.aligned ? '#ddd6fe' : '#e2e8f0');

      employeeNodes.append('text')
        .attr('class', 'employee-avatar-initial')
        .attr('x', d => -getSize(d).w / 2 + 30).attr('y', 1)
        .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
        .attr('font-size', '13px').attr('font-weight', '700')
        .attr('fill', d => d.data.aligned ? '#6d28d9' : '#64748b')
        .text(d => (d.data.name || '?').slice(0, 1));

      employeeNodes.append('line')
        .attr('x1', d => -getSize(d).w / 2 + 59).attr('x2', d => -getSize(d).w / 2 + 59)
        .attr('y1', d => -getSize(d).h / 2 + 11).attr('y2', d => getSize(d).h / 2 - 11)
        .attr('stroke', '#ede9fe').attr('stroke-width', 1);

      employeeNodes.append('text')
        .attr('class', 'employee-info-name')
        .attr('x', d => -getSize(d).w / 2 + 69).attr('y', -14)
        .attr('text-anchor', 'start').attr('font-size', '12px').attr('font-weight', '700')
        .attr('fill', '#1e293b')
        .text(d => _trim(d.data.name || '', getSize(d).maxChars));

      employeeNodes.append('text')
        .attr('class', 'employee-info-department')
        .attr('x', d => -getSize(d).w / 2 + 69).attr('y', 3)
        .attr('text-anchor', 'start').attr('font-size', '9px')
        .attr('fill', '#64748b')
        .text(d => _trim(d.data.subtitle || '未分配部门', 10));

      employeeNodes.append('rect')
        .attr('class', 'employee-progress-track')
        .attr('x', d => -getSize(d).w / 2 + 69).attr('y', 15)
        .attr('width', 58).attr('height', 5).attr('rx', 2.5)
        .attr('fill', '#d1fae5');

      employeeNodes.append('rect')
        .attr('class', 'employee-progress-value')
        .attr('x', d => -getSize(d).w / 2 + 69).attr('y', 15)
        .attr('width', d => 58 * _progressValue(d.data.progress) / 100)
        .attr('height', 5).attr('rx', 2.5).attr('fill', '#10b981');

      employeeNodes.append('text')
        .attr('class', 'employee-info-progress')
        .attr('x', d => -getSize(d).w / 2 + 134).attr('y', 20)
        .attr('text-anchor', 'start').attr('font-size', '9px').attr('font-weight', '700')
        .attr('fill', '#059669')
        .text(d => `${_progressValue(d.data.progress)}%`);

      otherNormalNodes.append('text')
        .attr('text-anchor', 'middle').attr('y', -6)
        .attr('font-size', d => getSize(d).fontSize + 'px')
        .attr('font-weight', '600')
        .attr('fill', d => {
          if (['company', 'department', 'dept-group', 'root'].includes(d.data.type)) return '#fff';
          if (d.data.type === 'placeholder') return '#94a3b8';
          return '#1e293b';
        })
        .text(d => {
          const name = d.data.name || '';
          const max = getSize(d).maxChars;
          return name.length > max ? name.slice(0, max) + '...' : name;
        });

      otherNormalNodes.append('text')
        .attr('text-anchor', 'middle').attr('y', 14)
        .attr('font-size', d => getSize(d).subSize + 'px')
        .attr('fill', d => {
          if (['company', 'department', 'dept-group', 'root'].includes(d.data.type)) return 'rgba(255,255,255,0.8)';
          if (d.data.type === 'placeholder') return '#94a3b8';
          return '#64748b';
        })
        .text(d => {
          if (d.data.type === 'placeholder') return d.data.subtitle || '未配置';
          if (d.data.type === 'dept-group') return d.data.subtitle || '';
          return `${Math.round(d.data.progress || 0)}%`;
        });

      // ── 用户节点状态标签（非 active 时显示） ──
      const statusBgColor = { draft: '#f1f5f9', pending: '#fef3c7', rejected: '#fef2f2' };

      normalNodes.filter(d => d.data._hasNonActive && statusLabel[d.data.status])
        .each(function (d) {
          const g = d3.select(this);
          const s = getSize(d);
          const label = statusLabel[d.data.status];
          const tw = label.length * 11 + 12;
          // 员工节点内容更紧凑，状态条需要完整悬浮在节点上方，避免遮住姓名。
          const badgeLift = d.data.type === 'personal' ? 18 : 6;
          g.append('rect')
            .attr('x', -tw / 2).attr('y', -s.h / 2 - badgeLift)
            .attr('width', tw).attr('height', 16)
            .attr('rx', 8)
            .attr('fill', statusBgColor[d.data.status])
            .attr('stroke', statusColor[d.data.status])
            .attr('stroke-width', 1);
          g.append('text')
            .attr('text-anchor', 'middle').attr('y', -s.h / 2 - badgeLift + 11)
            .attr('font-size', '9px').attr('font-weight', '600')
            .attr('fill', statusColor[d.data.status])
            .text(label);
        });

      // ── 展开/折叠按钮 ──
      const toggleNodes = nodes.filter(d => isCollapsible(d));
      const toggleG = toggleNodes.append('g')
        .attr('transform', d => `translate(${getSize(d).w / 2 + 14}, 0)`)
        .style('cursor', 'pointer')
        .on('click', function (event, d) {
          event.stopPropagation();
          const data = d.data;
          if (data.children && data.children.length > 0) {
            data._children = data.children;
            data.children = [];
          } else if (data._children && data._children.length > 0) {
            data.children = data._children;
            data._children = [];
          }
          update();
        });

      toggleG.append('circle')
        .attr('r', 10)
        .attr('fill', '#fff')
        .attr('stroke', '#8b5cf6')
        .attr('stroke-width', 1.5);

      toggleG.append('text')
        .attr('text-anchor', 'middle')
        .attr('dominant-baseline', 'central')
        .attr('font-size', '14px')
        .attr('font-weight', '700')
        .attr('fill', '#8b5cf6')
        .text(d => {
          const data = d.data;
          if (data.children && data.children.length > 0) return '−';
          const count = data._okrCount || (data._children ? data._children.length : 0);
          return count > 0 ? '+' : '';
        });

      // ── 对齐标记 ──
      nodes.filter(d => d.data.aligned)
        .append('text')
        .attr('x', d => getSize(d).w / 2 - 10)
        .attr('y', d => -getSize(d).h / 2 + 4)
        .attr('font-size', '12px')
        .text('🔗');

      // ── 员工节点备注入口（公司负责人/对应部门负责人） ──
      if (options.onEditEmployeeNote) {
        const noteG = nodes
          .filter(d => d.data.type === 'personal' && d.data.userId && d.data.canEditNote)
          .append('g')
          .attr('class', 'employee-note-icon')
          .attr('transform', d => `translate(${-getSize(d).w / 2 + 46}, ${getSize(d).h / 2 - 14})`)
          .attr('role', 'button')
          .attr('tabindex', 0)
          .attr('aria-label', d => `${d.data.note ? '修改' : '添加'}${d.data.name}的看板备注`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onEditEmployeeNote(d.data.userId, d.data.note || '', d.data.name || '员工');
          })
          .on('keydown', function(event, d) {
            if (!['Enter', ' '].includes(event.key)) return;
            event.preventDefault();
            event.stopPropagation();
            options.onEditEmployeeNote(d.data.userId, d.data.note || '', d.data.name || '员工');
          });
        noteG.append('circle')
          .attr('r', 7).attr('fill', '#fff').attr('stroke', '#c4b5fd').attr('stroke-width', 1);
        noteG.append('text')
          .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
          .attr('font-size', '8px').attr('fill', '#7c3aed').text('✎');
        noteG.append('title').text(d => d.data.note ? '修改员工备注' : '添加员工备注');
      }

      // ── 编辑图标（部门OKR节点） ──
      if (options.onEditDept) {
        const editG = nodes
          .filter(d => d.data.type === 'department' && d.data.id)
          .append('g')
          .attr('class', 'edit-icon')
          .attr('transform', d => `translate(${getSize(d).w / 2 - 10}, ${-getSize(d).h / 2 + 10})`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onEditDept(d.data.id);
          });
        editG.append('circle')
          .attr('r', 9)
          .attr('fill', 'rgba(255,255,255,0.92)')
          .attr('stroke', '#93c5fd')
          .attr('stroke-width', 1.5);
        editG.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '11px')
          .attr('fill', '#1d4ed8')
          .text('✎');
      }

      // ── 编辑图标（公司OKR节点） ──
      if (options.onEditCompany) {
        const editCG = nodes
          .filter(d => d.data.type === 'company' && d.data.id)
          .append('g')
          .attr('class', 'edit-icon')
          .attr('transform', d => `translate(${getSize(d).w / 2 - 10}, ${-getSize(d).h / 2 + 10})`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onEditCompany(d.data.id);
          });
        editCG.append('circle')
          .attr('r', 9)
          .attr('fill', 'rgba(255,255,255,0.92)')
          .attr('stroke', '#a78bfa')
          .attr('stroke-width', 1.5);
        editCG.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '11px')
          .attr('fill', '#6d28d9')
          .text('✎');
      }

      // ── 删除图标（公司OKR节点左上角） ──
      if (options.onDeleteCompany) {
        const delCG = nodes
          .filter(d => d.data.type === 'company' && d.data.id)
          .append('g')
          .attr('class', 'delete-icon')
          .attr('transform', d => `translate(${-getSize(d).w / 2 + 10}, ${-getSize(d).h / 2 + 10})`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onDeleteCompany(d.data.id);
          });
        delCG.append('circle')
          .attr('r', 9)
          .attr('fill', 'rgba(255,255,255,0.92)')
          .attr('stroke', '#fca5a5')
          .attr('stroke-width', 1.5);
        delCG.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '12px')
          .attr('font-weight', '700')
          .attr('fill', '#dc2626')
          .text('✕');
      }

      // ── 删除图标（部门OKR节点左上角） ──
      if (options.onDeleteDept) {
        const delDG = nodes
          .filter(d => d.data.type === 'department' && d.data.id)
          .append('g')
          .attr('class', 'delete-icon')
          .attr('transform', d => `translate(${-getSize(d).w / 2 + 10}, ${-getSize(d).h / 2 + 10})`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onDeleteDept(d.data.id);
          });
        delDG.append('circle')
          .attr('r', 9)
          .attr('fill', 'rgba(255,255,255,0.92)')
          .attr('stroke', '#fca5a5')
          .attr('stroke-width', 1.5);
        delDG.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '12px')
          .attr('font-weight', '700')
          .attr('fill', '#dc2626')
          .text('✕');
      }

      // ── 删除图标（个人OKR详情节点左上角，部门视图展开后可见） ──
      if (options.onDeletePersonal) {
        const delPG = nodes
          .filter(d => d.data.type === 'okr-detail' && d.data.id)
          .append('g')
          .attr('class', 'delete-icon')
          .attr('transform', d => `translate(${-getSize(d).w / 2 + 10}, ${-getSize(d).h / 2 + 10})`)
          .style('cursor', 'pointer')
          .on('click', function(event, d) {
            event.stopPropagation();
            options.onDeletePersonal(d.data.id);
          });
        delPG.append('circle')
          .attr('r', 9)
          .attr('fill', 'rgba(255,255,255,0.92)')
          .attr('stroke', '#fca5a5')
          .attr('stroke-width', 1.5);
        delPG.append('text')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'central')
          .attr('font-size', '12px')
          .attr('font-weight', '700')
          .attr('fill', '#dc2626')
          .text('✕');
      }
    }

    this._treeControllers.set(container.id, { rootData, update, options });
    update();
  },

  _countLeaves(node) {
    if (!node.children || node.children.length === 0) return 1;
    return node.children.reduce((sum, c) => sum + this._countLeaves(c), 0);
  },
};
