/* Member-level assessment presentation. Official scores always come from the API. */
const OkrMemberEvaluation = (() => {
  let members = new Map();
  let editing = null;
  let previousFocus = null;
  const exporting = new Set();
  const escape = value => _escapeAuditHTML(value ?? '');
  const score = value => value == null ? '待完成' : String(value);
  const statusLabels = {
    completed: '月度评估已完成', no_objectives: '尚未设置当月个人目标',
    pending_tasks: '任务目标尚未全部完成评估', pending_behavior: '待主管填写行为目标及红线系数',
  };

  function setMembers(items) {
    // A new month/company replaces the whole snapshot; no cross-scope cache.
    members = new Map(items.map(item => [Number(item.user_id), item]));
  }

  function summary(userId, editable = true) {
    const member = members.get(Number(userId));
    if (!member) return '';
    const evaluation = member.evaluation || {};
    const metric = (title, value, note, total = false) => `<div class="member-evaluation-metric${total ? ' is-total' : ''}">
      <div class="member-evaluation-label">${title}</div><div class="member-evaluation-value">${score(value)}</div>
      <div class="member-evaluation-note">${note}</div></div>`;
    const factor = member.redline_factor;
    const action = editable && member.can_evaluate
      ? `<button type="button" class="btn btn-primary btn-sm" onclick="OkrMemberEvaluation.open(${Number(userId)})">${member.evaluation ? '修改行为及红线评分' : '填写行为及红线评分'}</button>` : '';
    return `<section class="member-evaluation${factor === 0 ? ' is-redline' : ''}" aria-label="成员月度总评">
      <div class="member-evaluation-head"><div class="member-evaluation-title">${escape(member.year_month)} 月度总评</div>${action}</div>
      <div class="member-evaluation-grid">
        ${metric('任务目标 · 70%', member.task_contribution, `原始分 ${score(member.task_score)} · 已评 ${member.completed_objective_count}/${member.objective_count} 个 O`)}
        ${metric('行为目标 · 30%', member.behavior_contribution, `原始分 ${score(member.behavior_score)} · 两项各占 50%`)}
        ${metric('红线系数', factor, factor === 0 ? '存在重大失误或主观过错' : factor === 1 ? '无重大失误和主观过错' : '由主管确认，取值 0 或 1')}
        ${metric('月度总分', member.total_score, '满分 100 分', true)}
      </div>
      <div class="member-evaluation-detail">
        <div class="member-evaluation-behavior"><strong>内外部满意度 · 50%</strong><br>内外部满意度达到“满意”以上<br>评分：${score(evaluation.satisfaction_score)} / 100</div>
        <div class="member-evaluation-behavior"><strong>主动履职与结果意识 · 50%</strong><br>服从工作安排，主动开展工作；事事有交代，事事有回应；注重工作结果，而不是强调客观原因<br>评分：${score(evaluation.initiative_score)} / 100</div>
      </div>
      <div class="member-evaluation-note">总分 =（任务目标原始分 × 70% ＋ 行为目标原始分 × 30%）× 红线系数。任务原始分按各 O 权重归一化汇总。</div>
      ${evaluation.redline_reason ? `<div class="member-evaluation-note">红线说明：${escape(evaluation.redline_reason)}</div>` : ''}
      ${evaluation.comment ? `<div class="member-evaluation-note">评分说明：${escape(evaluation.comment)}</div>` : ''}
      <div class="member-evaluation-status">${statusLabels[member.status]}${evaluation.reviewer_name ? ` · 评分人：${escape(evaluation.reviewer_name)}` : ''}</div>
    </section>`;
  }

  function renderMy(userId) {
    const container = document.getElementById('myMonthlyEvaluation');
    if (container) container.innerHTML = summary(userId, false);
  }

  function exportButton(userId) {
    const member = members.get(Number(userId));
    if (!member || member.status !== 'completed') return '';
    return `<button type="button" class="btn btn-ghost btn-sm" onclick="OkrMemberEvaluation.exportPdf(${Number(userId)}, this)">导出评估表 PDF</button>`;
  }

  async function exportPdf(userId, button) {
    const member = members.get(Number(userId));
    if (!member || member.status !== 'completed') return;
    const key = `${member.year_month}:${member.user_id}`;
    if (exporting.has(key)) return;
    exporting.add(key);
    button.disabled = true;
    button.textContent = '正在导出…';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45000);
    try {
      // The shared api() wrapper expects JSON; this feature downloads binary PDF.
      const response = await fetch(`/api/okr/evaluations/monthly/${member.user_id}/pdf?period=${encodeURIComponent(member.year_month)}`, {
        headers: { Authorization: `Bearer ${getToken()}` }, signal: controller.signal,
      });
      if (response.status === 401) {
        clearAuth(); window.location.href = '/okr/login';
        throw new Error('登录已失效，请重新登录');
      }
      if (!response.ok) {
        const error = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : {};
        throw new Error(error.detail || '导出失败，请稍后重试');
      }
      if (!response.headers.get('Content-Type')?.includes('application/pdf')) {
        throw new Error('未收到有效的 PDF 文件，请重试');
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      const name = String(member.user_name || '员工').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0, 60);
      link.download = `${name}_${member.year_month}_目标评估表.pdf`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      toast(error.name === 'AbortError' ? '导出超时，请稍后重试' : error.message, 'error');
    } finally {
      clearTimeout(timeout);
      exporting.delete(key);
      button.disabled = false;
      button.textContent = '导出评估表 PDF';
    }
  }

  function ensureForm() {
    if (document.getElementById('memberEvaluationModal')) return;
    const mask = document.createElement('div');
    mask.id = 'memberEvaluationModal';
    mask.className = 'modal-mask';
    mask.setAttribute('role', 'dialog');
    mask.setAttribute('aria-modal', 'true');
    mask.setAttribute('aria-labelledby', 'memberEvaluationTitle');
    mask.innerHTML = `<form class="modal member-evaluation-form" id="memberEvaluationForm">
      <div class="modal-title" id="memberEvaluationTitle">行为目标与红线评分</div>
      <div class="member-evaluation-note" id="memberEvaluationContext"></div>
      <div class="form-group"><label for="memberSatisfaction">内外部满意度达到“满意”以上（行为目标权重 50%）</label>
        <input class="form-input" id="memberSatisfaction" type="number" min="0" max="100" step="1" required placeholder="请输入 0–100 的整数"></div>
      <div class="form-group"><label for="memberInitiative">服从工作安排，主动开展工作；事事有交代，事事有回应；注重工作结果，而不是强调客观原因（行为目标权重 50%）</label>
        <input class="form-input" id="memberInitiative" type="number" min="0" max="100" step="1" required placeholder="请输入 0–100 的整数"></div>
      <div class="form-group"><label for="memberRedline">红线系数</label><select class="form-input" id="memberRedline" required>
        <option value="">请选择</option><option value="1">1 — 期间无重大失误和主观过错</option><option value="0">0 — 存在重大失误或主观过错，总分归零</option></select></div>
      <div class="form-group"><label for="memberRedlineReason">红线说明（系数为 0 时必填）</label>
        <textarea class="form-input" id="memberRedlineReason" rows="3" maxlength="2000" placeholder="说明具体事实与判断依据"></textarea></div>
      <div class="form-group"><label for="memberEvaluationComment">评分说明</label>
        <textarea class="form-input" id="memberEvaluationComment" rows="3" maxlength="2000" placeholder="填写评分依据或反馈（可选）"></textarea></div>
      <div class="member-evaluation-error" id="memberEvaluationError" role="alert"></div>
      <div class="member-evaluation-note">每人每月保存一次行为与红线评分，修改会保留审计记录。所有个人 O 评估完成后生成正式总分。</div>
      <div class="modal-footer"><button type="button" class="btn btn-ghost" id="memberEvaluationCancel">取消</button>
        <button type="submit" class="btn btn-primary" id="memberEvaluationSubmit">保存评分</button></div>
    </form>`;
    document.body.appendChild(mask);
    document.getElementById('memberEvaluationCancel').addEventListener('click', close);
    document.getElementById('memberRedline').addEventListener('change', () => {
      document.getElementById('memberRedlineReason').required = document.getElementById('memberRedline').value === '0';
    });
    document.getElementById('memberEvaluationForm').addEventListener('submit', submit);
    mask.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key !== 'Tab') return;
      const controls = [...mask.querySelectorAll('input,select,textarea,button')].filter(el => !el.disabled);
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
  }

  function open(userId) {
    const member = members.get(Number(userId));
    if (!member || !member.can_evaluate) return;
    ensureForm();
    editing = member;
    previousFocus = document.activeElement;
    const evaluation = member.evaluation || {};
    document.getElementById('memberEvaluationContext').textContent = `${member.user_name} · ${member.year_month}`;
    document.getElementById('memberSatisfaction').value = evaluation.satisfaction_score ?? '';
    document.getElementById('memberInitiative').value = evaluation.initiative_score ?? '';
    document.getElementById('memberRedline').value = evaluation.redline_factor ?? '';
    document.getElementById('memberRedlineReason').value = evaluation.redline_reason || '';
    document.getElementById('memberRedlineReason').required = evaluation.redline_factor === 0;
    document.getElementById('memberEvaluationComment').value = evaluation.comment || '';
    document.getElementById('memberEvaluationError').textContent = '';
    document.getElementById('memberEvaluationSubmit').disabled = false;
    showModal('memberEvaluationModal');
    document.getElementById('memberSatisfaction').focus();
  }

  function close() {
    if (document.getElementById('memberEvaluationSubmit')?.disabled) return;
    hideModal('memberEvaluationModal');
    editing = null;
    if (previousFocus?.isConnected) previousFocus.focus();
  }

  async function submit(event) {
    event.preventDefault();
    if (!editing) return;
    if (!document.getElementById('memberEvaluationForm').reportValidity()) return;
    const member = editing;
    const button = document.getElementById('memberEvaluationSubmit');
    if (button.disabled) return;
    const error = document.getElementById('memberEvaluationError');
    const satisfaction = Number(document.getElementById('memberSatisfaction').value);
    const initiative = Number(document.getElementById('memberInitiative').value);
    const factor = Number(document.getElementById('memberRedline').value);
    const reason = document.getElementById('memberRedlineReason').value.trim();
    if (![satisfaction, initiative].every(value => Number.isInteger(value) && value >= 0 && value <= 100)) {
      error.textContent = '行为评分须为 0–100 的整数'; return;
    }
    if (![0, 1].includes(factor)) { error.textContent = '请选择红线系数'; return; }
    if (factor === 0 && !reason) { error.textContent = '红线系数为 0 时请填写说明'; return; }
    button.disabled = true;
    error.textContent = '';
    try {
      await api(`/api/okr/evaluations/monthly/${member.user_id}`, { method: 'PUT', body: {
        year_month: member.year_month, satisfaction_score: satisfaction, initiative_score: initiative,
        redline_factor: factor, redline_reason: reason,
        comment: document.getElementById('memberEvaluationComment').value.trim(),
        expected_revision: member.evaluation?.revision || 0,
      } });
      button.disabled = false;
      close();
      toast('行为及红线评分已保存', 'success');
      await loadReviews();
    } catch (e) {
      error.textContent = e.message;
    } finally { button.disabled = false; }
  }

  function printTable(rows) {
    if (!rows.length) return '<p>暂无数据</p>';
    const headers = Object.keys(rows[0]);
    return `<table><thead><tr>${headers.map(key => `<th>${escape(key)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(row => `<tr>${headers.map(key => `<td>${escape(row[key])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  }

  return { setMembers, summary, renderMy, exportButton, exportPdf, open, close, printTable };
})();
