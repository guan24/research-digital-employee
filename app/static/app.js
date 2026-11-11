const $ = id => document.getElementById(id);

let tasks = [];
let selected = '';
let detail = null;
let events = [];
let artifacts = [];
let activeFile = '';
let fileText = '';
let eventSource = null;
let eventPollTimer = null;
let previewMode = 'rendered';
let planVersions = [];

const names = {
  deleting: '正在删除', queued: '排队中', running: '运行中',
  retry_wait: '等待重试', paused: '已暂停', completed: '已完成',
  failed: '执行失败', cancelled: '已取消'
};

const eventNames = {
  task_created: '任务创建', task_started: 'Worker 领取任务',
  agent_execution_started: 'Agent 开始执行', agent_initial_run: '首次运行',
  agent_plan_started: '自主规划启动', research_plan_updated: 'Agent 规划调整', research_plan_finalized: '最终计划归档', checkpoint_resume: 'Checkpoint 恢复',
  checkpoint_already_complete: '检查点已完成', model_call_started: '模型调用开始',
  model_call_completed: '模型调用完成', model_call_failed: '模型调用失败',
  tool_call_started: '工具调用开始', tool_call_completed: '工具调用完成',
  tool_call_failed: '工具调用失败', research_search_completed: '网页搜索完成',
  research_page_fetched: '网页读取完成', research_page_fetch_failed: '网页读取失败',
  artifact_saved: '中间成果保存', report_materialized_from_final_answer: '最终报告生成',
  report_saved_by_agent: '最终报告保存', task_manually_requeued: '人工重新入队',
  retry_scheduled: '自动重试调度', execution_exception: '执行异常',
  task_failed: '任务失败', task_completed: '任务完成',
  worker_recovery_scheduled: 'Worker 恢复调度', task_cancelled: '任务取消'
};

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

const time = value => value ? new Date(value).toLocaleString() : '—';
const compact = (value, limit = 190) => {
  const text = String(value ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const first = text.split(/(?<=[。！？.!?])\s+/)[0] || text;
  return first.length > limit ? first.slice(0, limit).trimEnd() + '…' : first;
};

function pretty(value) {
  if (value === undefined || value === null || value === '') return '无记录';
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2); }
    catch { return value; }
  }
  try { return JSON.stringify(value, null, 2); }
  catch { return String(value); }
}

function parseObject(value) {
  if (value && typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch { return null; }
}

function formatUsage(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const input = usage.prompt_tokens ?? usage.input_tokens;
  const output = usage.completion_tokens ?? usage.output_tokens;
  const total = usage.total_tokens ?? ((Number(input) || 0) + (Number(output) || 0));
  if (total) return 'Token ' + Number(total).toLocaleString() +
    (input != null || output != null ? '（输入 ' + (input ?? '—') + ' / 输出 ' + (output ?? '—') + '）' : '');
  return '';
}

function durationLabel(payload = {}) {
  const ms = Number(payload.duration_ms);
  if (!Number.isFinite(ms) || ms < 0) return '';
  return ms >= 1000 ? (ms / 1000).toFixed(1) + ' 秒' : ms + ' ms';
}

async function req(url, options) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json; charset=utf-8', ...(options?.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw Error(body.detail || response.statusText);
  }
  return response.json();
}

async function refresh() {
  try {
    const [taskList, overview] = await Promise.all([
      req('/tasks?limit=100'), req('/system/overview')
    ]);
    tasks = taskList;
    const counts = overview.task_counts || {};
    $('total').textContent = tasks.length;
    $('running').textContent = counts.running || 0;
    $('completed').textContent = counts.completed || 0;
    $('failed').textContent = counts.failed || 0;
    $('worker').textContent = overview.worker.status === 'active' ? '● Worker 在线' : '○ Worker 离线';
    $('lease').textContent = overview.worker.worker_id || '等待 Worker 心跳';
    $('apiDot').style.background = '#65d39b';
    $('error').textContent = '';
    renderTasks();

    // Polling refreshes task metadata only. It must never replace the live trace DOM.
    if (selected) {
      try {
        detail = await req('/tasks/' + selected);
        renderTaskSummary();
      } catch { /* The task may have been deleted in another tab. */ }
    }
  } catch (error) {
    $('apiDot').style.background = '#ff7875';
    $('error').textContent = 'API 连接失败：' + error.message + '。请启动 FastAPI。';
  }
}

function renderTasks() {
  const query = $('search').value.toLowerCase();
  const filter = $('filter').value;
  const visible = tasks.filter(task =>
    (filter === 'all' || task.status === filter) &&
    (!query || (task.goal + ' ' + task.task_id).toLowerCase().includes(query))
  );
  $('taskCount').textContent = visible.length;
  $('tasks').innerHTML = visible.map(task =>
    '<div class="task ' + (task.task_id === selected ? 'selected' : '') +
    '" data-id="' + esc(task.task_id) + '">' +
    '<div class="meta"><span class="tag ' + esc(task.status) + '">' +
    esc(names[task.status] || task.status) + '</span>' + time(task.updated_at) + '</div>' +
    '<b>' + esc(task.goal) + '</b><div class="meta"><span>' +
    esc(task.task_id.slice(0, 8)) + '</span><span>尝试 ' +
    task.attempts + '/' + task.max_attempts + '</span></div></div>'
  ).join('') || '<div class="empty">暂无任务</div>';

  document.querySelectorAll('.task').forEach(element => {
    element.onclick = () => {
      selected = element.dataset.id;
      load(selected);
      renderTasks();
    };
  });
}

async function load(taskId) {
  try {
    if (eventSource) eventSource.close();
    const [task, history, files] = await Promise.all([
      req('/tasks/' + taskId),
      req('/tasks/' + taskId + '/events?limit=2000'),
      req('/tasks/' + taskId + '/artifacts')
    ]);
    selected = taskId;
    detail = task;
    events = history;
    artifacts = files.artifacts;
    activeFile = '';
    fileText = '';
    $('preview').textContent = '选择工作区文件预览';
    renderTaskSummary();
    renderTrace();
    renderArtifacts();
    renderPlan();
    connectEvents(taskId);
  } catch (error) {
    $('error').textContent = error.message;
  }
}

function connectEvents(taskId) {
  if (eventSource) eventSource.close();
  if (eventPollTimer) clearInterval(eventPollTimer);
  const lastId = events.length ? events[events.length - 1].event_id : 0;
  eventSource = new EventSource('/tasks/' + taskId + '/events/stream?after_id=' + lastId);
  eventSource.addEventListener('task_event', message => {
    const item = JSON.parse(message.data);
    if (events.some(event => event.event_id === item.event_id)) return;
    events.push(item);
    upsertTraceEvent(item);
    if (item.event_type.startsWith('research_plan_')) renderPlan();
    if (['artifact_saved', 'report_saved_by_agent', 'report_materialized_from_final_answer'].includes(item.event_type)) refreshArtifacts(taskId);
    if (['task_started', 'task_completed', 'task_failed', 'task_cancelled', 'retry_scheduled'].includes(item.event_type)) {
      req('/tasks/' + taskId).then(task => {
        if (selected === taskId) {
          detail = task;
          renderTaskSummary();
        }
      }).catch(() => {});
    }
  });
  eventSource.addEventListener('task_deleted', () => eventSource.close());
  eventSource.onerror = () => { /* EventSource reconnects with Last-Event-ID; polling is the fallback. */ };
  eventPollTimer = setInterval(() => pollNewEvents(taskId), 2500);
}

async function pollNewEvents(taskId) {
  if (selected !== taskId) return;
  const lastId = events.length ? events[events.length - 1].event_id : 0;
  try {
    const incoming = await req('/tasks/' + taskId + '/events?after_id=' + lastId + '&limit=200');
    for (const item of incoming) {
      if (events.some(event => event.event_id === item.event_id)) continue;
      events.push(item);
      upsertTraceEvent(item);
      if (item.event_type.startsWith('research_plan_')) renderPlan();
      if (['artifact_saved', 'report_saved_by_agent', 'report_materialized_from_final_answer'].includes(item.event_type)) refreshArtifacts(taskId);
    }
  } catch (error) { console.warn('Execution event polling failed', error); }
}

async function refreshArtifacts(taskId) {
  if (selected !== taskId) return;
  try {
    const result = await req('/tasks/' + taskId + '/artifacts');
    if (selected !== taskId) return;
    const oldPath = activeFile;
    artifacts = result.artifacts || [];
    renderArtifacts();
    if (oldPath && artifacts.some(item => item.path === oldPath)) await openFile(oldPath, false);
  } catch (error) { console.warn('Artifact refresh failed', error); }
}

function renderTaskSummary() {
  if (!detail) return;
  $('taskStatus').textContent = names[detail.status] || detail.status;
  $('detail').innerHTML =
    '<div class="detail"><div class="muted">' + esc(detail.task_id) + '</div>' +
    '<h3>' + esc(detail.goal) + '</h3><div class="kv"><span>尝试 ' +
    detail.attempts + '/' + detail.max_attempts + '</span><span>运行预算 ' +
    Math.round(detail.max_runtime_seconds / 60) + ' min</span><span>规划步数 ' +
    detail.max_steps + '</span></div>' +
    (detail.last_error ? '<p class="task-error">最近错误：' + esc(detail.last_error) + '</p>' : '') +
    '<div class="muted">创建：' + time(detail.created_at) + '　更新：' +
    time(detail.updated_at) + '</div></div>';

  $('pause').disabled = detail.status !== 'running';
  $('resume').disabled = detail.status !== 'paused';
  $('retry').disabled = detail.status !== 'failed';
  $('cancel').disabled = !['queued', 'retry_wait', 'running', 'paused'].includes(detail.status);
  $('delete').disabled = detail.status === 'deleting';
}

function renderArtifacts() {
  $('artifacts').innerHTML = artifacts.map(file =>
    '<div class="file ' + (activeFile === file.path ? 'active' : '') +
    '" data-file="' + esc(file.path) + '">▤　' + esc(file.path) +
    '　<small>' + Math.round(file.size_bytes / 1024 * 10) / 10 + ' KB</small></div>'
  ).join('') || '<div class="empty">Agent 尚未保存文件</div>';
  document.querySelectorAll('[data-file]').forEach(element => {
    element.onclick = () => openFile(element.dataset.file);
  });
}

function normalizedPlan(plan = {}) {
  return {
    title: String(plan.title || '研究执行计划'),
    objective: String(plan.objective || ''),
    steps: (Array.isArray(plan.steps) ? plan.steps : []).map((step, index) => ({
      id: String(step.id || 'step-' + (index + 1)),
      title: String(step.title || ''),
      objective: String(step.objective || ''),
      expected_output: String(step.expected_output || '')
    }))
  };
}

function planSnapshot() {
  const versions = [];
  for (const event of events) {
    if (event.event_type !== 'research_plan_updated') continue;
    const payload = event.payload || {};
    const plan = normalizedPlan(payload.plan || {});
    if (!plan.steps.length) continue;
    const signature = JSON.stringify(plan);
    // Collapse legacy versions created solely by step-status changes.
    if (versions.length && versions[versions.length - 1].signature === signature) continue;
    versions.push({
      version: versions.length + 1,
      plan,
      signature,
      event,
      changeType: payload.change_type || (versions.length ? 'replan' : 'initial'),
      trigger: payload.trigger || payload.change_reason || '',
      newInformation: payload.new_information || '',
      problemAnalysis: payload.problem_analysis || '',
      decision: payload.decision || '',
      changes: payload.changes || payload.change_reason || '',
      planDiff: payload.plan_diff || {}
    });
  }
  const latest = versions.length ? versions[versions.length - 1] : null;
  const finalizedEvent = latest
    ? [...events].reverse().find(event =>
        event.event_type === 'research_plan_finalized' &&
        event.event_id > latest.event.event_id
      ) || null
    : null;
  return {versions, latest, finalizedEvent};
}

function planHistoryEntry(version) {
  const label = version.changeType === 'initial' ? '初始规划' : '基于新信息重新规划';
  const fields = [
    ['触发原因', version.trigger],
    ['新获得的信息', version.newInformation],
    ['原计划的问题分析', version.problemAnalysis],
    ['调整决策', version.decision],
    ['具体调整', version.changes]
  ].filter(([, value]) => value);
  const rationale = fields.map(([name, value]) =>
    '<div class="plan-rationale-row"><b>' + esc(name) + '</b><p>' + esc(value) + '</p></div>'
  ).join('');
  const diff = Object.keys(version.planDiff || {}).length
    ? '<details class="plan-version-detail"><summary>查看相对上一版本的结构变化</summary><pre>' +
      esc(pretty(version.planDiff)) + '</pre></details>'
    : '';
  return '<section class="plan-history-entry"><div class="plan-history-heading"><b>v' +
    version.version + ' · ' + label + '</b><time>' + time(version.event.timestamp) +
    '</time></div>' + (rationale || '<p>根据用户目标建立整体研究路径。</p>') +
    diff + '<details class="plan-version-detail"><summary>查看该版本的完整计划</summary><pre>' +
    esc(pretty(version.plan)) + '</pre></details></section>';
}

function renderPlan() {
  const {versions, latest, finalizedEvent} = planSnapshot();
  planVersions = versions;
  const root = $('planPanel');
  if (!latest) {
    root.innerHTML = '<div class="empty plan-empty">Agent 尚未提交整体研究计划。计划生成后将在此处展示，实质性调整将形成新版本。</div>';
    return;
  }
  const archiveLabel = finalizedEvent ? '最终计划已归档' : '规划执行中';
  const archiveNote = finalizedEvent
    ? '<p class="plan-archive-note">任务结束：' + esc(finalizedEvent.payload?.outcome || '已结束') +
      ' · 完整历史已保存至 <code>notes/agent_execution_plan.json</code></p>'
    : '<p class="plan-archive-note">步骤进度、工具调用和异常请查看下方执行轨迹；本区域只记录整体策略及实质性调整。</p>';
  root.innerHTML = '<div class="plan-top"><div><strong>' + esc(latest.plan.title) +
    '</strong><p>' + esc(latest.plan.objective) + '</p></div><span class="plan-version">' +
    archiveLabel + ' · v' + latest.version + '</span></div>' +
    '<div class="plan-section-label">当前整体执行计划</div><ol class="plan-steps">' +
    latest.plan.steps.map((step, index) =>
      '<li class="plan-step"><span class="plan-index">' + (index + 1) +
      '</span><div><strong>' + esc(step.title) + '</strong>' +
      (step.objective ? '<p>' + esc(step.objective) + '</p>' : '') +
      (step.expected_output ? '<small>预期成果：' + esc(step.expected_output) + '</small>' : '') +
      '</div></li>'
    ).join('') + '</ol>' + archiveNote +
    '<details class="plan-history" ' + (versions.length > 1 ? '' : 'hidden') +
    '><summary>查看规划演进历史（' + versions.length + ' 个有效版本）</summary>' +
    versions.map(planHistoryEntry).join('') + '</details>' +
    (latest.event.payload.original_plan_text
      ? '<details class="plan-version-detail"><summary>查看 Agent 提交的原始计划</summary><pre>' +
        esc(latest.event.payload.original_plan_text) + '</pre></details>' : '');
}

function traceKey(event) {
  const payload = event.payload || {};
  if (event.event_type.startsWith('model_call_') && payload.run_id) return 'model:' + payload.run_id;
  if (event.event_type.startsWith('tool_call_') && payload.run_id) return 'tool:' + payload.run_id;
  return 'event:' + event.event_id;
}

function traceItems() {
  const grouped = new Map();
  for (const event of events) {
    if (['research_plan_step_status', 'research_plan_activity'].includes(event.event_type)) continue;
    const key = traceKey(event);
    let item = grouped.get(key);
    if (!item) {
      const kind = key.startsWith('model:') ? 'model' : key.startsWith('tool:') ? 'tool' : 'event';
      item = { key, kind, first: event, start: null, end: null, event };
      grouped.set(key, item);
    }
    item.event = event;
    if (event.event_type.endsWith('_started')) item.start = event;
    else if (event.event_type.endsWith('_completed') || event.event_type.endsWith('_failed')) item.end = event;
    if (!item.first || event.event_id < item.first.event_id) item.first = event;
  }
  return Array.from(grouped.values()).sort((a, b) => b.first.event_id - a.first.event_id);
}

function traceStatus(item) {
  if (!item.end) return item.start ? 'running' : 'info';
  return item.end.event_type.endsWith('_failed') ? 'failed' : 'success';
}

function summarizeToolOutput(value) {
  const parsed = parseObject(value);
  if (!parsed) return compact(value, 190) || '工具已返回结果';
  if (parsed.success === false) return compact(parsed.error || parsed.message || parsed.error_type, 190) || '工具执行失败';
  if (Array.isArray(parsed.results)) {
    return '检索到 ' + parsed.results.length + ' 条结果' +
      (parsed.saved_to ? ' · 已保存 ' + parsed.saved_to : '');
  }
  if (parsed.success === true) {
    return compact(parsed.title || parsed.url || parsed.saved_to || parsed.message, 190) || '操作成功';
  }
  const parts = [];
  if (parsed.result_count != null) parts.push('返回 ' + parsed.result_count + ' 条结果');
  if (parsed.title) parts.push(parsed.title);
  if (parsed.saved_to || parsed.artifact) parts.push('已保存 ' + (parsed.saved_to || parsed.artifact));
  if (parsed.url) parts.push(parsed.url);
  return parts.join(' · ') || compact(value, 190) || '工具已返回结果';
}

function genericSummary(event) {
  const payload = event.payload || {};
  switch (event.event_type) {
    case 'agent_plan_started':
      return 'Agent 已接收研究目标，正在制定整体研究策略。';
    case 'research_plan_updated':
      return payload.change_type === 'initial'
        ? 'Agent 制定初始整体执行计划，共 ' + (payload.plan?.steps?.length || 0) + ' 个研究步骤。'
        : 'Agent 根据新信息重新规划：' + compact(payload.trigger || payload.change_reason, 160);
    case 'research_plan_finalized':
      return '任务结束，最终执行计划及 ' + (payload.history_count || 0) + ' 个规划版本已归档。';
    case 'checkpoint_resume':
      return '从检查点继续执行' + (payload.pending_nodes?.length ? ' · 待执行节点：' + payload.pending_nodes.join(', ') : '');
    case 'checkpoint_already_complete':
      return '检查点显示研究流程已完成，正在恢复最终结果。';
    case 'research_search_completed':
      return '检索：' + compact(payload.query, 110) + ' · ' + (payload.result_count ?? 0) + ' 条结果' +
        (payload.artifact ? ' · ' + payload.artifact : '');
    case 'research_page_fetched':
      return '已读取：' + compact(payload.title || payload.url, 150) +
        (payload.char_count ? ' · ' + payload.char_count + ' 字符' : '');
    case 'research_page_fetch_failed':
      return compact(payload.error || payload.error_type || payload.url, 190);
    case 'artifact_saved':
    case 'report_saved_by_agent':
    case 'report_materialized_from_final_answer':
      return '成果文件：' + (payload.path || payload.artifact || '已保存');
    case 'execution_exception':
    case 'task_failed':
      return compact(payload.error || payload.message || payload.reason, 190) || '执行过程发生异常';
    case 'retry_scheduled':
      return '将在稍后重试' + (payload.delay_seconds ? ' · 等待 ' + payload.delay_seconds + ' 秒' : '');
    case 'task_manually_requeued':
      return compact(payload.reason, 190) || '任务已重新入队';
    default: {
      const fields = ['message', 'reason', 'note', 'pending_nodes', 'path', 'artifact', 'result_count'];
      const values = fields.filter(key => payload[key] != null).map(key =>
        key + '：' + (Array.isArray(payload[key]) ? payload[key].join(', ') : compact(payload[key], 100))
      );
      return values.join(' · ') || (eventNames[event.event_type] || event.event_type);
    }
  }
}

function traceCard(item) {
  const event = item.end || item.start || item.event;
  const payload = event.payload || {};
  const startPayload = item.start?.payload || {};
  const endPayload = item.end?.payload || {};
  const status = traceStatus(item);
  const isModel = item.kind === 'model';
  const isTool = item.kind === 'tool';
  let category = isModel ? '模型' : isTool ? '工具' : '系统';
  let title = isModel ? '模型调用' : isTool ? '工具调用' : (eventNames[event.event_type] || event.event_type);
  let summary = '';
  let meta = [];
  let details = '';

  if (isModel) {
    const model = startPayload.model || endPayload.model || 'LLM';
    title += ' · ' + model;
    if (status === 'failed') summary = compact(endPayload.error, 190) || '模型调用失败';
    else if (item.end) summary = compact(endPayload.decision_summary, 210) || '模型已返回结果';
    else summary = compact(startPayload.input_summary, 150) || '模型正在处理研究请求';
    if (startPayload.purpose) meta.push(startPayload.purpose);
    const duration = durationLabel(endPayload);
    if (duration) meta.push(duration);
    const usage = formatUsage(endPayload.usage);
    if (usage) meta.push(usage);
    const request = startPayload.raw_request;
    const response = endPayload.raw_response;
    if (request || response) {
      details = '<details class="trace-details"><summary>查看完整请求 / 响应</summary><div class="trace-detail-grid">' +
        '<section><h5>模型请求（Prompt）</h5><pre>' + esc(pretty(request)) + '</pre></section>' +
        '<section><h5>模型响应</h5><pre>' + esc(pretty(response)) + '</pre></section></div></details>';
    }
  } else if (isTool) {
    const toolName = startPayload.tool || endPayload.tool || 'unknown tool';
    title += ' · ' + toolName;
    if (status === 'failed') summary = compact(endPayload.error, 190) || '工具调用失败';
    else if (item.end) summary = summarizeToolOutput(endPayload.output);
    else summary = compact(startPayload.input, 170) || '工具正在执行';
    const duration = durationLabel(endPayload);
    if (duration) meta.push(duration);
    const input = startPayload.input;
    const output = endPayload.output;
    const error = endPayload.error;
    if (input || output || error) {
      details = '<details class="trace-details"><summary>查看工具参数 / 返回结果</summary><div class="trace-detail-grid">' +
        '<section><h5>工具参数</h5><pre>' + esc(pretty(input)) + '</pre></section>' +
        '<section><h5>工具返回</h5><pre>' + esc(pretty(output || error)) + '</pre></section></div></details>';
    }
  } else {
    summary = genericSummary(event);
    if (payload.error || payload.exception || status === 'failed') status = 'failed';
    if (payload.duration_ms != null) meta.push(durationLabel(payload));
    if (payload.attempt != null) meta.push('尝试 ' + payload.attempt);
    if (Object.keys(payload).length) {
      details = '<details class="trace-details"><summary>查看事件数据</summary><pre>' +
        esc(pretty(payload)) + '</pre></details>';
    }
  }

  const statusLabel = status === 'success' ? '完成' : status === 'failed' ? '失败' : status === 'running' ? '进行中' : '记录';
  const statusText = status === 'failed' && !isModel && !isTool ? '异常' : statusLabel;
  return '<article class="trace-card trace-' + status + '" data-trace-key="' + esc(item.key) + '">' +
    '<div class="trace-rail"><span></span></div><div class="trace-body">' +
    '<div class="trace-heading"><div class="trace-title"><span class="trace-category">' + category +
    '</span><strong>' + esc(title) + '</strong><span class="trace-status">' + statusText +
    '</span></div><time>' + time(item.first.timestamp) + '</time></div>' +
    (summary ? '<p class="trace-summary">' + esc(summary) + '</p>' : '') +
    (meta.length ? '<div class="trace-meta">' + meta.map(value => '<span>' + esc(value) + '</span>').join('') + '</div>' : '') +
    details + '</div></article>';
}

function renderTrace() {
  $('events').innerHTML = traceItems().map(traceCard).join('') || '<div class="empty">等待 Agent 开始执行…</div>';
  $('eventCount').textContent = events.length + ' EVENTS';
}

function upsertTraceEvent(event) {
  const item = traceItems().find(candidate => candidate.key === traceKey(event));
  if (!item) return;
  const container = $('events');
  const existing = Array.from(container.querySelectorAll('.trace-card'))
    .find(element => element.dataset.traceKey === item.key);
  const next = document.createRange().createContextualFragment(traceCard(item)).firstElementChild;

  if (existing) {
    const oldDetails = existing.querySelector('details');
    const newDetails = next.querySelector('details');
    if (oldDetails && newDetails) {
      const wasOpen = oldDetails.open;
      const scrollPositions = Array.from(oldDetails.querySelectorAll('pre')).map(node => node.scrollTop);
      oldDetails.innerHTML = newDetails.innerHTML;
      oldDetails.open = wasOpen;
      oldDetails.querySelectorAll('pre').forEach((node, index) => {
        node.scrollTop = scrollPositions[index] || 0;
      });
      newDetails.replaceWith(oldDetails);
    }
    existing.replaceWith(next);
  } else {
    const oldHeight = container.scrollHeight;
    const oldTop = container.scrollTop;
    container.prepend(next);
    if (oldTop > 0) container.scrollTop = oldTop + (container.scrollHeight - oldHeight);
  }
  $('eventCount').textContent = events.length + ' EVENTS';
}

async function openFile(path, rerender = true) {
  try {
    const response = await fetch('/tasks/' + selected + '/artifacts/' + path);
    if (!response.ok) throw Error('读取文件失败');
    fileText = await response.text();
    activeFile = path;
    renderArtifactPreview();
    if (rerender) renderArtifacts();
  } catch (error) { alert(error.message); }
}

function renderMarkdown(source) {
  const lines = String(source || '').replace(/\r\n/g, '\n').split('\n');
  const output = [];
  let inCode = false, code = [], listType = null;
  const closeList = () => { if (listType) { output.push(listType === 'ol' ? '</ol>' : '</ul>'); listType = null; } };
  const inline = value => esc(value)
    .replace(/\x60([^\x60]+)\x60/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^ )]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const isTableSeparator = line => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const tableCells = line => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith(String.fromCharCode(96).repeat(3))) {
      closeList();
      if (inCode) { output.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>'); code = []; }
      inCode = !inCode; continue;
    }
    if (inCode) { code.push(line); continue; }
    if (!line.trim()) { closeList(); continue; }
    if (index + 1 < lines.length && line.includes('|') && isTableSeparator(lines[index + 1])) {
      closeList();
      const headers = tableCells(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        rows.push(tableCells(lines[index]));
        index += 1;
      }
      index -= 1;
      output.push('<div class="preview-table-wrap"><table><thead><tr>' +
        headers.map(cell => '<th>' + inline(cell) + '</th>').join('') +
        '</tr></thead><tbody>' + rows.map(row => '<tr>' +
        headers.map((_, column) => '<td>' + inline(row[column] || '') + '</td>').join('') +
        '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (heading) { closeList(); const level = heading[1].length; output.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>'); }
    else if (unordered || ordered) {
      const nextType = unordered ? 'ul' : 'ol';
      if (listType !== nextType) { closeList(); listType = nextType; output.push('<' + listType + '>'); }
      output.push('<li>' + inline((unordered || ordered)[1]) + '</li>');
    } else if (/^>\s?/.test(line)) { closeList(); output.push('<blockquote>' + inline(line.replace(/^>\s?/, '')) + '</blockquote>'); }
    else if (/^---+$/.test(line.trim())) { closeList(); output.push('<hr>'); }
    else { closeList(); output.push('<p>' + inline(line) + '</p>'); }
  }
  closeList();
  if (inCode) output.push('<pre><code>' + esc(code.join('\n')) + '</code></pre>');
  return output.join('\n');
}

function renderArtifactPreview() {
  const preview = $('preview');
  const isJson = activeFile.toLowerCase().endsWith('.json');
  const isMarkdown = activeFile.toLowerCase().endsWith('.md');

  // JSON is shown as plain source text: no parsing, pretty-printing or highlighting.
  if (isMarkdown) {
    $('previewRendered').hidden = false;
    $('previewSource').hidden = false;
    preview.innerHTML = previewMode === 'rendered'
      ? renderMarkdown(fileText)
      : '<pre class="source-code">' + esc(fileText) + '</pre>';
    $('previewRendered').classList.toggle('active', previewMode === 'rendered');
    $('previewSource').classList.toggle('active', previewMode === 'source');
    $('previewRendered').disabled = previewMode === 'rendered';
    $('previewSource').disabled = previewMode === 'source';
    $('previewModeHint').textContent = previewMode === 'rendered'
      ? 'Markdown · 文档渲染' : 'Markdown · 原始源码';
  } else {
    $('previewRendered').hidden = true;
    $('previewSource').hidden = true;
    preview.innerHTML = '<pre class="source-code">' + esc(fileText) + '</pre>';
    $('previewModeHint').textContent = isJson ? 'JSON · 原始文本' : '文本 · 原始内容';
  }
}

async function action(name) {
  if (!detail) return;
  try {
    await req('/tasks/' + detail.task_id + '/' + name, {
      method: 'POST',
      body: JSON.stringify(name === 'retry' ? { reason: '通过研究控制台手动重试' } : {})
    });
    await refresh();
  } catch (error) { alert(error.message); }
}

async function deleteSelected() {
  if (!detail) return;
  const taskId = detail.task_id;
  const running = detail.status === 'running';
  const message = running
    ? '任务仍在运行，将请求取消并等待最多 60 秒后彻底删除。确定继续？'
    : '将彻底删除任务、执行事件、检查点和工作区，且无法恢复。确定继续？';
  if (!confirm(message)) return;
  const button = $('delete');
  button.disabled = true;
  button.textContent = running ? '正在停止并清理…' : '正在删除…';
  try {
    await req('/tasks/' + taskId, { method: 'DELETE' });
    if (eventSource) eventSource.close();
    selected = '';
    detail = null;
    events = [];
    artifacts = [];
    fileText = '';
    $('preview').textContent = '选择工作区文件预览';
    $('events').innerHTML = '<div class="empty">选择任务查看执行轨迹</div>';
    await refresh();
  } catch (error) { alert(error.message); }
  finally {
    button.disabled = false;
    button.textContent = '删除任务';
  }
}

$('previewRendered').onclick = () => { previewMode = 'rendered'; renderArtifactPreview(); };
$('previewSource').onclick = () => { previewMode = 'source'; renderArtifactPreview(); };
$('delete').onclick = deleteSelected;
$('refresh').onclick = refresh;
$('search').oninput = renderTasks;
$('filter').onchange = renderTasks;
$('newTask').onclick = () => $('createDialog').showModal();
$('closeDialog').onclick = () => $('createDialog').close();

$('createForm').onsubmit = async event => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    const task = await req('/tasks', {
      method: 'POST',
      body: JSON.stringify({
        goal: form.get('goal'),
        max_steps: Number(form.get('max_steps')),
        max_runtime_seconds: Number(form.get('max_runtime_seconds')),
        max_attempts: Number(form.get('max_attempts'))
      })
    });
    $('createDialog').close();
    event.target.reset();
    selected = task.task_id;
    await load(selected);
    await refresh();
  } catch (error) { alert(error.message); }
};

['pause', 'resume', 'cancel', 'retry'].forEach(name => {
  $(name).onclick = () => {
    if (name === 'cancel' || name === 'retry') {
      if (!confirm(name === 'cancel' ? '确认取消任务？' : '重新执行失败任务并尝试恢复 Checkpoint？')) return;
    }
    action(name);
  };
});

$('download').onclick = () => {
  if (!fileText) return;
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(new Blob([fileText], { type: 'text/markdown' }));
  anchor.download = activeFile || 'report.md';
  anchor.click();
  URL.revokeObjectURL(anchor.href);
};

refresh();
setInterval(refresh, 3000);
