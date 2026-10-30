"""Render a member evaluation snapshot as a portable, paginated Chinese PDF."""

from datetime import datetime, timezone
from html import escape
from threading import Lock

import fitz


# MuPDF rendering is serialized within each application worker.
_render_lock = Lock()


def _text(value):
    return escape(str(value if value is not None else "-"), quote=True).replace("\n", "<br>")


def _number(value):
    return f"{float(value):g}" if value is not None else "-"


def _report_html(member):
    evaluation = member["evaluation"]
    metrics = [
        ("任务目标 · 70%", member["task_contribution"], f"原始分 {_number(member['task_score'])}"),
        ("行为目标 · 30%", member["behavior_contribution"], f"原始分 {_number(member['behavior_score'])}"),
        ("红线系数", member["redline_factor"], "取值 0 或 1"),
        ("月度总分", member["total_score"], "满分 100 分"),
    ]
    metric_cells = "".join(
        f'<td><div class="muted">{_text(label)}</div><div class="score">{_number(value)}</div>'
        f'<div class="muted">{_text(note)}</div></td>' for label, value, note in metrics
    )
    tasks = []
    for index, objective in enumerate(member["objectives"], 1):
        kr_rows = "".join(
            f'<tr><td>KR{kr_index} · {_text(kr["title"])}</td>'
            f'<td>{_number(kr["current_value"])}/{_number(kr["target_value"])} {_text(kr["unit"] or "")}</td>'
            f'<td>{_number(kr["progress"])}%</td></tr>'
            for kr_index, kr in enumerate(objective.get("key_results", []), 1)
        )
        weight = float(objective["objective_weight"]) / member["total_objective_weight"] * 100
        tasks.append(f'''<h3>O{index} · {_text(objective['objective_title'])}</h3>
          <p>{_text(objective.get('description') or '未填写目标描述')}</p>
          <p class="muted">目标权重：{_number(objective['objective_weight'])}（归一化占比 {weight:.1f}%）</p>
          <table><tr><th>关键结果</th><th width="24%">实际值 / 目标值</th><th width="15%">完成率</th></tr>
          {kr_rows or '<tr><td colspan="3">暂无关键结果</td></tr>'}</table>
          <p class="task-scores">KR 完成率：{_number(objective['kr_completion_rate'])}%　
          自评分：{_number(objective['self_score'])}　主管评分：{_number(objective['manager_score'])}　
          O 综合得分：{_number(objective['composite_score'])}</p>
          <p><b>自评说明：</b>{_text(objective.get('self_comment') or '未填写')}<br>
          <b>主管评语：</b>{_text(objective.get('manager_comment') or '未填写')}</p>''')
    return f'''<html><body>
      <h1>员工月度目标评估表</h1>
      <p class="company">{_text(member['company_name'])}</p>
      <table class="identity"><tr><td>姓名：{_text(member['user_name'])}</td>
      <td>部门：{_text(member['department_name'])}</td><td>考核月份：{_text(member['year_month'])}</td></tr></table>
      <h2>一、月度总评</h2>
      <table class="metrics"><tr>{metric_cells}</tr></table>
      <p class="muted">月度总分 =（任务原始分 × 70% + 行为原始分 × 30%）× 红线系数。</p>
      <p class="muted">任务原始分按当月个人 O 权重汇总；O 综合得分 = 自评分 × 20% + 主管评分 × 80%。KR 完成率仅展示，不参与计分。</p>
      <h2>二、行为目标与红线评估</h2>
      <table><tr><th>行为目标</th><th class="numeric">权重</th><th class="numeric">评分</th></tr>
      <tr><td>内外部满意度达到“满意”以上</td><td class="numeric">50%</td><td class="numeric">{evaluation['satisfaction_score']}</td></tr>
      <tr><td>服从工作安排，主动开展工作；事事有交代，事事有回应；注重工作结果，而不是强调客观原因</td>
      <td class="numeric">50%</td><td class="numeric">{evaluation['initiative_score']}</td></tr></table>
      <p class="muted">两项行为评分均为 0-100 分，分别占行为目标的 50%。</p>
      <p><b>红线判定：</b>{'期间无重大失误和主观过错' if member['redline_factor'] == 1 else '存在重大失误或主观过错，月度总分归零'}</p>
      <p><b>红线说明：</b>{_text(evaluation.get('redline_reason') or '无')}</p>
      <p><b>评分说明：</b>{_text(evaluation.get('comment') or '未填写')}</p>
      <p class="muted">评分人：{_text(evaluation.get('reviewer_name') or '已删除用户')}　
      最近评分时间（UTC）：{_text(evaluation['updated_at'])}　版本：{evaluation['revision']}</p>
      <h2>三、任务目标明细（{member['objective_count']} 个 O）</h2>
      {''.join(tasks)}
      <p class="muted end-note">本表依据导出时的目标进度、权重及已完成评估生成；后续修改可能影响再次导出的结果。
      导出时间（UTC）：{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}</p>
      </body></html>'''


_CSS = """
body { font-family: sans-serif; font-size: 10pt; line-height: 1.5; color: #25324a; }
h1 { font-size: 21pt; text-align: center; color: #6325cf; margin: 0 0 6pt; }
.company { text-align: center; color: #64748b; margin: 0 0 18pt; }
h2 { font-size: 13pt; color: #6325cf; margin: 18pt 0 9pt; page-break-after: avoid; }
h3 { font-size: 11pt; margin: 15pt 0 5pt; page-break-after: avoid; }
p { margin: 5pt 0; }
table { width: 100%; border-collapse: collapse; margin: 7pt 0; }
th, td { border: 0.6pt solid #dbe3ed; padding: 7pt; vertical-align: top; }
th { text-align: left; font-size: 9pt; }
.numeric { width: 55pt; white-space: nowrap; }
.identity td { font-size: 9pt; }
.metrics td { width: 25%; }
.score { font-size: 22pt; font-weight: bold; color: #6325cf; margin: 3pt 0; }
.muted { font-size: 8.5pt; color: #64748b; }
.task-scores { padding: 6pt; font-size: 9pt; }
.end-note { margin-top: 16pt; border-top: 0.6pt solid #dbe3ed; padding-top: 8pt; }
"""


def build_member_evaluation_pdf(member):
    """The caller supplies an authorized, completed snapshot; no I/O or data writes."""
    if member["status"] != "completed":
        raise ValueError("月度总评尚未完成，无法导出评估表")
    page_rect = fitz.paper_rect("a4")
    content_rect = page_rect + (38, 38, -38, -38)

    def next_page(page_number, _filled):
        if page_number >= 100:
            raise ValueError("评估内容超过 100 页，无法导出")
        return page_rect, content_rect, None

    with _render_lock:
        story = fitz.Story(html=_report_html(member), user_css=_CSS)
        with story.write_with_links(next_page) as document:
            for index, page in enumerate(document, 1):
                page.insert_text((38, page.rect.height - 22),
                                 f"iTrial OKR | {member['year_month']} | {index} / {len(document)}",
                                 fontsize=8, color=(0.4, 0.45, 0.5))
            document.set_metadata({"title": f"{member['user_name']} {member['year_month']} 目标评估表",
                                   "author": "iTrial OKR"})
            document.subset_fonts()
            return document.tobytes(garbage=4, deflate=True)
