/**
 * OKR 统计图表组件 (ECharts)
 */

const OKRCharts = {
  _escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[char]));
  },

  _memberDisplayName(item) {
    if (!item.duplicate_name) return item.user_name || '';
    const source = item.account_source === 'wecom' ? '企业微信' : '未绑定企业微信';
    return `${item.user_name || ''}（${source}）`;
  },

  /**
   * 获取或创建 ECharts 实例（避免重复 init 导致切换异常）
   */
  _getChart(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return null;
    const existing = echarts.getInstanceByDom(container);
    if (existing) return existing;
    container.innerHTML = '';          // 仅首次 init 前清除占位文字
    return echarts.init(container);
  },

  /**
   * 清空容器中的图表实例并显示占位文字
   */
  _showEmpty(containerId, msg) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const old = echarts.getInstanceByDom(container);
    if (old) old.dispose();
    container.innerHTML = `<div style="text-align:center;padding:60px;color:#94a3b8;">${msg || '暂无数据'}</div>`;
  },

  /**
   * 渲染各部门完成率柱状图
   * @param {string} containerId
   * @param {Array} data - [{department_name, avg_progress, total_objectives, completed_objectives}]
   */
  renderOverviewBar(containerId, data) {
    if (!data || data.length === 0) {
      this._showEmpty(containerId, '暂无数据');
      return;
    }

    const chart = this._getChart(containerId);
    if (!chart) return;

    chart.setOption({
      title: { text: '各部门 OKR 平均完成率', left: 'center', textStyle: { fontSize: 15 } },
      tooltip: {
        trigger: 'axis',
        formatter: p => {
          const d = p[0];
          const item = data[d.dataIndex];
          return `${d.name}<br/>平均完成率: ${d.value}%<br/>目标总数: ${item.total_objectives}<br/>已完成: ${item.completed_objectives}`;
        },
      },
      xAxis: {
        type: 'category',
        data: data.map(d => d.department_name),
        axisLabel: { rotate: data.length > 6 ? 30 : 0, fontSize: 12 },
      },
      yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' } },
      series: [{
        type: 'bar',
        data: data.map(d => d.avg_progress),
        barWidth: '40%',
        itemStyle: {
          borderRadius: [6, 6, 0, 0],
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#8b5cf6' },
            { offset: 1, color: '#6d28d9' },
          ]),
        },
        label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11 },
      }],
      grid: { left: 60, right: 30, bottom: 50, top: 60 },
    }, true);  // true = notMerge，完全替换旧配置
  },

  /**
   * 渲染跨部门对比雷达图
   */
  renderCompareRadar(containerId, data) {
    if (!data || data.length === 0) {
      this._showEmpty(containerId, '暂无数据');
      return;
    }

    const chart = this._getChart(containerId);
    if (!chart) return;

    const indicator = data.map(d => ({
      name: d.department_name,
      max: 100,
    }));

    chart.setOption({
      title: { text: '跨部门 OKR 对比', left: 'center', textStyle: { fontSize: 15 } },
      tooltip: {},
      radar: {
        indicator,
        radius: '60%',
        axisName: { fontSize: 11 },
      },
      series: [{
        type: 'radar',
        data: [{
          value: data.map(d => d.avg_progress),
          name: 'OKR 完成率',
          areaStyle: { color: 'rgba(109,40,217,0.15)' },
          lineStyle: { color: '#6d28d9', width: 2 },
          itemStyle: { color: '#6d28d9' },
        }],
      }],
    }, true);
  },

  /**
   * 渲染部门内成员贡献饼图
   */
  renderMemberPie(containerId, data) {
    if (!data || data.length === 0) {
      this._showEmpty(containerId, '暂无数据');
      return;
    }

    const chart = this._getChart(containerId);
    if (!chart) return;

    chart.setOption({
      title: { text: '成员 OKR 完成情况', left: 'center', textStyle: { fontSize: 15 } },
      tooltip: { trigger: 'item', formatter: '{b}: {c}% ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['35%', '60%'],
        data: data.filter(d => d.total_objectives > 0).map(d => ({
          name: this._memberDisplayName(d),
          value: d.avg_progress,
        })),
        label: { fontSize: 11 },
        emphasis: {
          itemStyle: { shadowBlur: 10, shadowColor: 'rgba(0,0,0,0.2)' },
        },
        itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
      }],
      color: ['#6d28d9', '#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#f97316'],
    }, true);
  },

  /**
   * 渲染部门成员 OKR 提交情况表格
   * @param {string} containerId
   * @param {Array} data - [{user_name, submitted_objectives, pending_objectives, completed_objectives}]
   * @param {string} deptName - 部门名称
   */
  renderMemberTable(containerId, data, deptName) {
    const container = document.getElementById(containerId);
    if (!container) return;
    // 先销毁可能存在的 ECharts 实例
    const old = echarts.getInstanceByDom(container);
    if (old) old.dispose();

    if (!data || data.length === 0) {
      container.innerHTML = '<div style="text-align:center;padding:60px;color:#94a3b8;">暂无数据</div>';
      return;
    }

    let rows = '';
    data.forEach(d => {
      const noOkr = d.submitted_objectives === 0;
      const rowStyle = noOkr ? ' style="background:#fef2f2;color:#dc2626"' : '';
      const displayName = this._escapeHtml(this._memberDisplayName(d));
      rows += `<tr${rowStyle}>
        <td>${displayName}</td>
        <td>${d.submitted_objectives}</td>
        <td>${d.pending_objectives}</td>
        <td>${d.completed_objectives ?? 0}</td>
      </tr>`;
    });

    container.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead>
          <tr style="background:#f1f5f9;color:#475569">
            <th style="padding:8px 12px;text-align:left;font-weight:600;border-bottom:2px solid #e2e8f0">姓名</th>
            <th style="padding:8px 12px;text-align:center;font-weight:600;border-bottom:2px solid #e2e8f0">已提交</th>
            <th style="padding:8px 12px;text-align:center;font-weight:600;border-bottom:2px solid #e2e8f0">审核中</th>
            <th style="padding:8px 12px;text-align:center;font-weight:600;border-bottom:2px solid #e2e8f0">已完成</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  },
};
