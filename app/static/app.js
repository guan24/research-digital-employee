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
let loadGeneration = 0;
let agentProfiles = [];
let workerOverview = { status: 'offline' };
let eventStreamMode = 'connecting';
let lastBudget = null;
let researchState = null;
let planSnapshotData = null;
let planSnapshotTaskId = '';

const names = {
  deleting: '正在删除', queued: '排队中', running: '运行中',
  retry_wait: '等待重试', paused: '已暂停', completed: '已完成',
  failed: '执行失败', cancelled: '已取消', waiting_for_human: '等待人工决策'
};

const eventNames = {
  task_created: '任务创建', budget_warning: '预算预警', budget_restricted: '预算进入受限状态', budget_soft_limit_reached: '预算达到软上限', budget_extension_approved: '预算扩额已批准', budget_checkpoint_resume_requested: '扩额后请求断点续跑', delivery_validation_completed: '交付校验完成', delivery_repair_resumed: '定向修复恢复', research_memory_saved: '研究记忆保存', research_finding_recorded: '研究发现登记', task_started: 'Worker 领取任务',
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
  human_intervention_requested: '请求人工决策', human_intervention_resolved: '人工决策已提交',
  research_controller_parked_for_human: '研究控制器暂停等待人工决策',
  research_quality_assessed: '研究质量评估', recovery_decision_recorded: '恢复决策记录',
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

async function loadAgents() {
  try {
    const result = await req('/agents');
    agentProfiles = Array.isArray(result.agents) ? result.agents : [];
    const select = $('agentType');
    if (select) {
      const selectedType = select.value;
      select.innerHTML = agentProfiles.map(agent => '<option value="' + esc(agent.agent_type) + '">' +
        esc(agent.display_name || agent.name || agent.agent_type) + '（' + esc(agent.agent_type) + '）</option>').join('') ||
        '<option value="research">研究员（research）</option>';
      if (agentProfiles.some(agent => agent.agent_type === selectedType)) select.value = selectedType;
      renderAgentDefaults();
      renderTasks();
      if (detail) renderTaskSummary();
    }
  } catch (error) { if ($('agentDefaults')) $('agentDefaults').textContent = '岗位列表加载失败：' + error.message; }
}
function getAgent(agentType) { return agentProfiles.find(agent => agent.agent_type === agentType) || null; }
function roleLabel(agentType) {
  const agent = getAgent(agentType);
  return agent ? (agent.display_name || agent.name || agent.agent_type) : (agentType || '研究岗位');
}
function renderAgentDefaults() {
  const agent = getAgent($('agentType')?.value || 'research'), root = $('agentDefaults');
  if (!root) return;
  if (!agent) { root.textContent = '未读取到岗位默认预算；留空由服务端岗位配置决定。'; return; }
  root.innerHTML = '<b>岗位默认配额</b><span>工具调用 ' + esc(agent.max_steps) + ' 次</span><span>Token ' +
    Number(agent.max_total_tokens || 0).toLocaleString() + '</span><span>运行时长 ' + esc(agent.max_runtime_seconds) + ' 秒</span>';
}
async function refresh(manual = false) {
  if (document.visibilityState === 'hidden' && !manual) return;
  const [taskResult, overviewResult] = await Promise.allSettled([req('/tasks?limit=100'), req('/system/overview')]);
  if (taskResult.status === 'fulfilled') {
    tasks = taskResult.value;
    $('total').textContent = tasks.length;
    $('running').textContent = tasks.filter(task => task.status === 'running').length;
    $('completed').textContent = tasks.filter(task => task.status === 'completed').length;
    $('failed').textContent = tasks.filter(task => ['failed','retry_wait'].includes(task.status)).length;
    renderTasks();
  } else $('error').textContent = '任务列表加载失败：' + taskResult.reason.message;
  if (overviewResult.status === 'fulfilled') {
    workerOverview = overviewResult.value.worker || {status:'offline'};
    $('worker').textContent = workerOverview.status === 'active' ? '● Worker 在线' :
      workerOverview.status === 'lease_expired' ? '! Worker 租约过期' : '○ Worker 离线';
    $('lease').textContent = workerOverview.worker_id || '当前没有持有有效租约';
    $('apiDot').style.background = '#65d39b';
  } else {
    $('apiDot').style.background = '#ff7875';
    $('error').textContent = '运行状态读取失败：' + overviewResult.reason.message;
  }
  if (selected && (manual || taskResult.status === 'fulfilled')) {
    const taskId = selected;
    try {
      detail = await req('/tasks/' + taskId);
      if (selected === taskId) {
        renderTaskSummary();
        await loadResearchState(taskId);
        if (manual) await load(taskId);
      }
    } catch (error) { if (manual) $('detail').innerHTML = '<div class="load-error">任务详情刷新失败：' + esc(error.message) + '</div>'; }
  }
}

function taskRuntimeSeconds(task) {
  let used = Number(task.runtime_seconds_used || 0);
  if (task.status === 'running' && task.heartbeat_at) used += Math.max(0, Date.now()/1000 - Number(task.heartbeat_at));
  return used;
}
function formatDuration(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds)||0));
  if (value < 60) return value + ' 秒';
  if (value < 3600) return Math.floor(value/60) + ' 分钟';
  return Math.floor(value/3600) + ' 小时 ' + Math.floor(value%3600/60) + ' 分钟';
}
function heartbeatAge(task) { return task?.heartbeat_at ? Math.max(0,Date.now()/1000-Number(task.heartbeat_at)) : Infinity; }
function taskBudgetRatio(task) {
  return Math.max(
    Number(task.max_steps) ? Number(task.execution_steps||0)/Number(task.max_steps) : 0,
    Number(task.max_total_tokens) ? Number(task.total_tokens_used||0)/Number(task.max_total_tokens) : 0,
    Number(task.max_runtime_seconds) ? taskRuntimeSeconds(task)/Number(task.max_runtime_seconds) : 0
  );
}
function taskIsStale(task) { return task.status === 'running' && heartbeatAge(task) > 30; }
function renderTasks() {
  const query = $('search').value.toLowerCase(), filter = $('filter').value, sort = $('sort').value;
  const visible = tasks.filter(task => {
    const ratio = taskBudgetRatio(task), abnormal = ['failed','retry_wait'].includes(task.status) || taskIsStale(task);
    const budgetAlert = task.budget_enabled !== false && ratio >= .8, role = roleLabel(task.agent_type);
    const matches = filter === 'all' || (filter === 'abnormal' && abnormal) ||
      (filter === 'budget_alert' && budgetAlert) || task.status === filter;
    return matches && (!query || (task.goal+' '+task.task_id+' '+role+' '+task.agent_type).toLowerCase().includes(query));
  });
  visible.sort((a,b) => sort === 'activity' ? String(b.updated_at||'').localeCompare(String(a.updated_at||'')) :
    String(b.created_at||'').localeCompare(String(a.created_at||'')));
  $('taskCount').textContent = visible.length;
  $('tasks').innerHTML = visible.map(task => {
    const ratio=taskBudgetRatio(task), stale=taskIsStale(task);
    return '<button type="button" class="task '+(task.task_id===selected?'selected':'')+'" data-id="'+esc(task.task_id)+'">'+
      '<div class="meta"><span class="tag '+esc(task.status)+'">'+esc(names[task.status]||task.status)+'</span>'+
      '<span>'+esc(roleLabel(task.agent_type))+' · '+esc(task.agent_type||'research')+'</span></div>'+
      '<b>'+esc(compact(task.goal,130)||'未命名任务')+'</b><div class="meta"><span>'+
      (task.status==='running'?'已运行 '+formatDuration(taskRuntimeSeconds(task)):'活动 '+time(task.updated_at))+
      '</span><span>尝试 '+esc(task.attempts)+'/'+esc(task.max_attempts)+'</span></div><div class="task-flags">'+
      (ratio>=.8&&task.budget_enabled!==false?'<span class="flag warning">预算 '+Math.round(ratio*100)+'%</span>':'')+
      (stale?'<span class="flag danger">心跳过期</span>':'')+'</div></button>';
  }).join('') || '<div class="empty"><b>还没有匹配的任务</b><p>创建任务 → 查看自主计划 → 跟踪执行轨迹与证据。</p><button type="button" class="primary" id="emptyCreate">创建第一个任务</button></div>';
  $('emptyCreate')?.addEventListener('click',()=>$('createDialog').showModal());
  document.querySelectorAll('.task[data-id]').forEach(element=>element.addEventListener('click',()=>{
    selected=element.dataset.id;load(selected);renderTasks();
  }));
}

async function load(taskId) {
  const generation = ++loadGeneration;
  selected = taskId;
  if (eventSource) eventSource.close();
  if (eventPollTimer) clearInterval(eventPollTimer);
  eventSource = null; eventPollTimer = null;
  detail = null; events = []; artifacts = []; activeFile = ''; fileText = ''; lastBudget = null;
  planSnapshotData = null; planSnapshotTaskId = '';
  $('taskStatus').textContent = '加载中';
  setStreamStatus('');
  researchState = null;
  $('researchControlPanel').innerHTML = '<div class="empty">正在读取研究控制与人工干预状态…</div>';
  $('qualityPanel').innerHTML = '<div class="empty">正在读取交付校验记录…</div>';
  $('detail').innerHTML = '<div class="empty">正在读取任务执行实例…</div>';
  $('instanceDiagnostics').innerHTML = '';
  $('events').innerHTML = '<div class="empty">正在读取执行事件…</div>';
  $('eventCount').textContent = '加载中';
  $('planPanel').innerHTML = '<div class="empty">正在读取 Agent 执行计划…</div>';
  $('artifacts').innerHTML = '<div class="empty">正在读取工作区文件…</div>';
  const [taskResult, historyResult, artifactResult, planResult] = await Promise.allSettled([
    req('/tasks/' + taskId), req('/tasks/' + taskId + '/events?limit=2000'),
    req('/tasks/' + taskId + '/artifacts'), req('/tasks/' + taskId + '/plan')
  ]);
  if (generation !== loadGeneration || selected !== taskId) return;
  if (taskResult.status === 'fulfilled') { detail = taskResult.value; renderTaskSummary(); }
  else {
    $('taskStatus').textContent = '详情加载失败';
    $('detail').innerHTML = '<div class="load-error"><strong>任务实例详情读取失败</strong><p>' +
      esc(taskResult.reason?.message || '未知 API 错误') + '</p></div>';
  }
  if (historyResult.status === 'fulfilled') {
    events = Array.isArray(historyResult.value) ? historyResult.value : [];
    renderTrace(); renderPlan(); renderQuality(); renderHealth(); renderInstanceDiagnostics();
  } else {
    events = [];
    $('events').innerHTML = '<div class="load-error"><strong>执行事件读取失败</strong><p>' +
      esc(historyResult.reason?.message || '未知 API 错误') + '</p></div>';
    $('eventCount').textContent = '读取失败';
    renderPlan(); renderQuality(); renderHealth(); renderInstanceDiagnostics();
  }
  if (artifactResult.status === 'fulfilled') { artifacts = artifactResult.value.artifacts || []; renderArtifacts(); }
  else $('artifacts').innerHTML = '<div class="load-error">工作区文件读取失败：' +
    esc(artifactResult.reason?.message || '未知 API 错误') + '</div>';
  if (planResult.status === 'fulfilled') {
    planSnapshotData = planResult.value;
    planSnapshotTaskId = taskId;
    renderPlan();
  } else {
    planSnapshotData = null;
    planSnapshotTaskId = '';
  }
  await loadResearchState(taskId);
  if (taskResult.status === 'fulfilled' || historyResult.status === 'fulfilled') connectEvents(taskId);
  renderTasks();
}

async function refreshPlanSnapshot(taskId) {
  try {
    const snapshot = await req('/tasks/' + taskId + '/plan');
    if (selected !== taskId) return;
    planSnapshotData = snapshot;
    planSnapshotTaskId = taskId;
    renderPlan();
  } catch (error) {
    if (selected === taskId && planSnapshotTaskId !== taskId) {
      $('planPanel').innerHTML = '<div class="load-error">执行计划快照读取失败：' + esc(error.message) + '</div>';
    }
  }
}

function setStreamStatus(message,degraded=false) {
  const root=$('streamStatus');root.hidden=!message;root.textContent=message||'';root.classList.toggle('degraded',Boolean(degraded));
}
function startEventPolling(taskId) {
  if(eventPollTimer)clearInterval(eventPollTimer);
  eventPollTimer=setInterval(()=>{if(document.visibilityState==='visible')pollNewEvents(taskId);},2500);
  setStreamStatus('已降级为轮询 · 每 2.5 秒检查新增事件',true);
}
function connectEvents(taskId) {
  if(eventSource)eventSource.close();if(eventPollTimer)clearInterval(eventPollTimer);
  const lastId=events.length?events[events.length-1].event_id:0;
  eventStreamMode='connecting';setStreamStatus('正在建立实时事件连接…');
  eventSource=new EventSource('/tasks/'+taskId+'/events/stream?after_id='+lastId);
  eventSource.onopen=()=>{if(selected!==taskId)return;eventStreamMode='live';setStreamStatus('实时事件流已连接');};
  eventSource.addEventListener('task_event',message=>{
    let item;try{item=JSON.parse(message.data);}catch(error){console.error('Invalid task event payload',error);return;}
    if(events.some(event=>event.event_id===item.event_id))return;
    events.push(item);upsertTraceEvent(item);
    if(item.event_type.startsWith('research_plan_'))refreshPlanSnapshot(taskId);
    if(['research_control_decision','research_quality_assessed','recovery_decision_recorded',
        'human_intervention_requested','human_intervention_resolved','research_controller_parked_for_human'].includes(item.event_type))loadResearchState(taskId);
    if(['budget_warning','budget_restricted','budget_soft_limit_reached','budget_extension_approved','research_memory_saved','research_finding_recorded'].includes(item.event_type))renderBudgetAndEvidence();
    if(item.event_type==='delivery_validation_completed')renderQuality();
    renderInstanceDiagnostics();renderHealth();
    if(['artifact_saved','report_saved_by_agent','report_materialized_from_final_answer','artifact_materialized_from_final_answer'].includes(item.event_type))refreshArtifacts(taskId);
    if(['task_started','task_completed','task_failed','task_cancelled','retry_scheduled','budget_checkpoint_resume_requested','human_intervention_requested','human_intervention_resolved','research_controller_parked_for_human'].includes(item.event_type)){
      req('/tasks/'+taskId).then(task=>{if(selected===taskId){detail=task;renderTaskSummary();renderTasks();}}).catch(()=>{});
    }
  });
  eventSource.addEventListener('task_deleted',()=>eventSource.close());
  eventSource.onerror=()=>{
    if(selected!==taskId||eventStreamMode==='polling')return;
    eventStreamMode='polling';eventSource.close();eventSource=null;startEventPolling(taskId);
  };
}
async function pollNewEvents(taskId) {
  if(selected!==taskId||document.visibilityState==='hidden')return;
  const lastId=events.length?events[events.length-1].event_id:0;
  try{
    const incoming=await req('/tasks/'+taskId+'/events?after_id='+lastId+'&limit=200');
    for(const item of incoming){
      if(events.some(event=>event.event_id===item.event_id))continue;
      events.push(item);upsertTraceEvent(item);
      if(item.event_type.startsWith('research_plan_'))refreshPlanSnapshot(taskId);
      if(['research_control_decision','research_quality_assessed','recovery_decision_recorded',
          'human_intervention_requested','human_intervention_resolved','research_controller_parked_for_human'].includes(item.event_type))loadResearchState(taskId);
      if(['budget_warning','budget_restricted','budget_soft_limit_reached','budget_extension_approved','research_memory_saved','research_finding_recorded'].includes(item.event_type))renderBudgetAndEvidence();
      if(item.event_type==='delivery_validation_completed')renderQuality();
      if(['artifact_saved','report_saved_by_agent','report_materialized_from_final_answer','artifact_materialized_from_final_answer'].includes(item.event_type))refreshArtifacts(taskId);
      renderHealth();
    }
    if(incoming.length){const updated=await req('/tasks/'+taskId);if(selected===taskId){detail=updated;renderTaskSummary();renderTasks();}}
  }catch(error){setStreamStatus('事件轮询失败：'+error.message,true);}
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

function errorCategoryLabel(category) {
  return ({retryable:'可重试',terminal:'终态',unknown:'未知',control:'控制信号'})[category]||'未知';
}
function errorCategoryClass(category) { return category==='terminal'?'terminal':category==='retryable'?'retryable':'unknown'; }
function latestValidation() { return [...events].reverse().find(event=>event.event_type==='delivery_validation_completed')||null; }
function renderTaskSummary() {
  if(!detail)return;
  $('taskStatus').textContent=names[detail.status]||detail.status;
  const firstError=parseObject(detail.first_error_json),history=parseObject(detail.error_history_json)||[];
  const latestError=history.length?history[history.length-1]:null,plan=planSnapshot(),version=plan.latest?'v'+plan.latest.version:'尚未生成';
  const failed=['failed','retry_wait'].includes(detail.status),goal=String(detail.goal||'');
  $('detail').innerHTML='<div class="identity-card"><div class="identity-top"><span class="role-pill">'+esc(roleLabel(detail.agent_type))+
    '</span><code>'+esc(detail.agent_type||'research')+'</code><span class="tag '+esc(detail.status)+'">'+esc(names[detail.status]||detail.status)+'</span>'+
    '<span class="plan-version">当前计划 '+esc(version)+'</span></div><div class="identity-goal"><b>任务目标</b><p>'+esc(compact(goal,230))+'</p>'+
    (goal.length>230?'<details><summary>展开完整研究目标</summary><p>'+esc(goal)+'</p></details>':'')+'</div>'+
    '<div class="identity-metrics"><span>已运行 <b id="elapsedRuntime">'+esc(formatDuration(taskRuntimeSeconds(detail)))+'</b></span>'+
    '<span>尝试 <b>'+esc(detail.attempts)+'/'+esc(detail.max_attempts)+'</b></span>'+
    '<span>'+(detail.resume_mode?'<b class="resume-mark">断点续跑中</b>':'常规执行')+'</span>'+
    '<span>工具调用上限 <b>'+esc(detail.max_steps)+' 次</b></span></div>'+
    (failed?'<section class="failure-summary"><div class="failure-heading"><span class="error-class '+errorCategoryClass(detail.error_category||firstError?.category)+'">'+
      esc(errorCategoryLabel(detail.error_category||firstError?.category))+'</span><b>'+esc(detail.error_code||latestError?.error_code||'UNKNOWN_EXCEPTION')+
      '</b></div><p><strong>首次失败原因：</strong>'+esc(firstError?.summary||firstError?.message||detail.last_error||'未记录首因')+'</p>'+
      (latestError?'<small>最近一次错误：'+esc(latestError.summary||'')+'</small>':'')+
      '<details class="error-history"><summary>错误历史（'+history.length+' 条）</summary><ol>'+history.map((item,index)=>
        '<li><b>第 '+esc(item.attempt||index+1)+' 次 · '+esc(errorCategoryLabel(item.category))+'</b><code>'+
        esc(item.error_code||'UNKNOWN')+'</code><time>'+esc(time(item.timestamp))+'</time><p>'+esc(item.summary||'')+'</p></li>').join('')+
      '</ol></details>'+(detail.status==='retry_wait'?'<p class="retry-next">下次重试：'+esc(detail.next_run_at?time(Number(detail.next_run_at)*1000):'等待调度')+'</p>':'')+
      '</section>':'')+'<div class="muted">创建：'+esc(time(detail.created_at))+'　开始：'+esc(time(detail.started_at))+
      '　完成：'+esc(time(detail.completed_at))+'</div></div>';
  renderHealth();renderBudgetAndEvidence();renderQuality();renderInstanceDiagnostics();
  $('pause').disabled=detail.status!=='running';$('resume').disabled=detail.status!=='paused';
  $('retry').disabled=detail.status!=='failed';$('cancel').disabled=!['queued','retry_wait','running','paused'].includes(detail.status);
  $('delete').disabled=detail.status==='deleting';
}
function renderHealth() {
  if(!detail)return;
  const age=heartbeatAge(detail),stale=detail.status==='running'&&age>30,online=workerOverview.status==='active';
  $('taskWorker').textContent=detail.worker_id?(online?'在线 · ':'已分配 · ')+detail.worker_id:(online?'在线（空闲/其他任务）':'离线');
  $('heartbeatFreshness').textContent=Number.isFinite(age)?(age<60?Math.floor(age)+' 秒前':Math.floor(age/60)+' 分钟前'):'暂无心跳';
  $('heartbeatFreshness').classList.toggle('stale',stale);
  $('recoveryCount').textContent=events.filter(event=>['checkpoint_resume','worker_recovery_scheduled','checkpoint_already_complete','budget_checkpoint_resume_requested'].includes(event.event_type)).length+' 次';
  $('budgetState').textContent=budgetStateLabel(lastBudget?.state,detail);
  const elapsed=$('elapsedRuntime');if(elapsed)elapsed.textContent=formatDuration(taskRuntimeSeconds(detail));
}
function budgetStateLabel(state,task=detail) {
  if(task?.status==='failed'&&/BUDGET_EXHAUSTED/.test(task.error_code||''))return '已终止';
  if(state==='normal'&&events.some(event=>event.event_type==='budget_extension_approved'))return '已扩额';
  return ({normal:'正常',warning:'预警',restricted:'受限收敛',soft_limit:'已达软上限',disabled:'预算闸门关闭',missing:'不可用'})[state]||'待读取';
}

function renderBudgetAndEvidence() {
  if(!detail)return;const taskId=detail.task_id;
  Promise.allSettled([req('/tasks/'+taskId+'/budget'),req('/tasks/'+taskId+'/evidence'),req('/tasks/'+taskId+'/memory')]).then(results=>{
    if(selected!==taskId)return;
    const [budgetResult,evidenceResult,memoryResult]=results;
    if(budgetResult.status==='fulfilled'){
      const budget=budgetResult.value;lastBudget=budget;$('budgetState').textContent=budgetStateLabel(budget.state,detail);
      const labels={tool_calls:'工具调用',runtime_seconds:'运行时长',tokens:'Token'};
      const stopped=detail.status==='failed'&&/BUDGET_EXHAUSTED/.test(detail.error_code||'');
      const dimName={TOOL_STEP_BUDGET_EXHAUSTED:'工具调用',TOKEN_BUDGET_EXHAUSTED:'Token',RUNTIME_BUDGET_EXHAUSTED:'运行时长'};
      const title=stopped?'已终止 · '+(dimName[detail.error_code]||'预算')+'预算耗尽':budgetStateLabel(budget.state,detail);
      $('budgetPanel').innerHTML='<div class="budget-heading"><b>任务预算闸门</b><span class="budget-state state-'+esc(stopped?'terminated':budget.state)+'">'+esc(title)+'</span></div>'+
        Object.entries(budget.dimensions||{}).map(([key,item])=>{
          const ratio=Number(item.ratio||0),pct=Math.round(ratio*100),tone=ratio>=1?'over':ratio>=.95?'restricted':ratio>=.8?'warning':'normal';
          const used=key==='runtime_seconds'?formatDuration(item.used):Number(item.used||0).toLocaleString();
          const limit=key==='runtime_seconds'?formatDuration(item.limit):Number(item.limit||0).toLocaleString();
          const remaining=key==='runtime_seconds'?formatDuration(item.remaining):Number(item.remaining||0).toLocaleString();
          const reserved=key==='tokens'?'<small>已预留 '+Number(item.reserved||0).toLocaleString()+' Token</small>':'';
          return '<div class="budget-dimension"><div class="budget-dimension-top"><b>'+labels[key]+'</b><strong>'+pct+'%</strong></div>'+
            '<div class="budget-track '+tone+'"><i style="width:'+Math.min(100,pct)+'%"></i></div><div class="budget-numbers">'+
            '<span>已用 '+esc(used)+'</span><span>上限 '+esc(limit)+'</span><span>剩余 '+esc(remaining)+'</span></div>'+reserved+'</div>';
        }).join('')+((budget.state==='restricted'||budget.state==='soft_limit'||stopped)?
          '<p class="budget-instruction">探索工具已禁用，仅可复用已有证据并完成报告。</p>':'')+
          '<div class="budget-actions"><small>剩余量已扣除未结算 Token 预留。</small><button type="button" id="requestBudgetExtension">申请扩额</button></div>';
      $('requestBudgetExtension').addEventListener('click',()=>$('budgetDialog').showModal());
    }else{$('budgetState').textContent='读取失败';$('budgetPanel').innerHTML='<div class="load-error">预算面板加载失败：'+esc(budgetResult.reason?.message||'未知错误')+'</div>';}
    if(evidenceResult.status==='fulfilled')renderEvidence(evidenceResult.value);
    else $('evidencePanel').innerHTML='<div class="panelhead">证据登记簿</div><div class="load-error">证据登记簿加载失败：'+esc(evidenceResult.reason?.message||'未知错误')+'</div>';
    if(memoryResult.status==='fulfilled'){
      const memory=memoryResult.value;$('memoryPanel').innerHTML='<div class="panelhead">长期研究记忆</div>'+
        (memory.content?'<pre>'+esc(memory.content)+'</pre>':'<small>Agent 尚未保存研究记忆</small>');
    }else $('memoryPanel').innerHTML='<div class="load-error">研究记忆加载失败：'+esc(memoryResult.reason?.message||'未知错误')+'</div>';
  });
}
function safeHttpUrl(value) {
  try { const url = new URL(value); return ['http:','https:'].includes(url.protocol) ? url.href : '#'; }
  catch { return '#'; }
}
function sourceDomain(url) { try{return new URL(safeHttpUrl(url)).hostname.replace(/^www\./,'');}catch{return '未知域名';} }
function renderEvidence(evidence) {
  const sources=Array.isArray(evidence.sources)?evidence.sources:[],findings=Array.isArray(evidence.findings)?evidence.findings:[];
  const validation=latestValidation()?.payload||{},coverage=validation.evidence_coverage;
  $('evidencePanel').innerHTML='<div class="panelhead">证据登记簿 <span>'+sources.length+' 个来源</span></div>'+
    (sources.length?'<div class="evidence-summary"><span>来源 '+esc(evidence.source_count||sources.length)+'</span><span>已核验 '+
      esc(evidence.verified_source_count||0)+'</span><span>研究发现 '+esc(evidence.finding_count||findings.length)+'</span><span>报告证据覆盖率 '+
      (coverage==null?'未记录':Math.round(Number(coverage)*100)+'%')+'</span></div>'+
      '<div class="table-wrap"><table class="evidence-table"><thead><tr><th>来源编号</th><th>标题 / 域名</th><th>类型</th><th>核验状态</th><th>快照</th></tr></thead><tbody>'+
      sources.map(source=>'<tr><td><code>'+esc(source.source_id||'—')+'</code></td><td><b>'+esc(compact(source.title||source.url,90))+
        '</b><small>'+esc(sourceDomain(source.url))+'</small><a href="'+esc(safeHttpUrl(source.url))+'" target="_blank" rel="noopener noreferrer">原始来源 ↗</a></td><td>'+
        (/pdf/i.test(source.source_type||'')||/\.pdf(?:$|\?)/i.test(source.url||'')?'PDF':'网页')+'</td><td><span class="verify-state '+
        (source.verified?'verified':'unverified')+'">'+(source.verified?'已核验':'未核验')+'</span></td><td><button type="button" class="snapshot-open" data-snapshot-source="'+
        esc(source.source_id||'')+'" '+(source.source_id?'':'disabled')+'>查看快照</button></td></tr>').join('')+
      '</tbody></table></div>'+(findings.length?'<div class="finding-list"><b>研究发现</b>'+findings.map(finding=>
        '<article><p>'+esc(compact(finding.claim,240))+'</p><small>关联来源：'+esc((finding.source_ids||[]).join('、'))+
        ' · 置信度：'+esc(({low:'低',medium:'中',high:'高'})[finding.confidence]||finding.confidence||'未记录')+
        '</small></article>').join('')+'</div>':'<p class="muted">尚未登记研究发现。</p>'):
      '<div class="empty"><b>尚未登记证据</b><p>Agent 完成搜索和网页读取后，会将来源、核验状态与快照登记在这里。</p></div>');
  bindEvidenceSnapshotActions();
}

function bindEvidenceSnapshotActions() {
  const panel = $('evidencePanel');
  if (panel.dataset.snapshotActionsBound) return;
  panel.dataset.snapshotActionsBound = 'true';
  panel.addEventListener('click', event => {
    const button = event.target.closest('button[data-snapshot-source]');
    if (!button || !panel.contains(button) || button.disabled) return;
    openEvidenceSnapshot(button.dataset.snapshotSource, button);
  });
}

async function openEvidenceSnapshot(sourceId, button) {
  const taskId = selected;
  if (!taskId || !sourceId) return;
  const previousText = button.textContent;
  button.disabled = true;
  button.textContent = '读取中…';
  $('preview').innerHTML = '<div class="empty">正在读取证据快照 ' + esc(sourceId) + '…</div>';
  try {
    const response = await fetch(
      '/tasks/' + encodeURIComponent(taskId) + '/evidence/sources/' +
      encodeURIComponent(sourceId) + '/snapshot'
    );
    if (!response.ok) {
      let detail = '';
      try { detail = (await response.json()).detail || ''; } catch {}
      throw Error(detail || ('读取证据快照失败：HTTP ' + response.status));
    }
    const content = await response.text();
    if (selected !== taskId) return;
    fileText = content;
    activeFile = 'evidence/sources/' + sourceId + '.md';
    renderArtifactPreview();
    $('previewModeHint').textContent = '证据快照 · ' + sourceId + ' · SHA-256 已核验';
    renderArtifacts();
    $('preview').scrollIntoView({ behavior: 'smooth', block: 'center' });
    button.textContent = '已打开';
  } catch (error) {
    if (selected === taskId) $('preview').innerHTML = '<div class="load-error">' + esc(error.message) + '</div>';
    button.textContent = '重试查看';
  } finally {
    button.disabled = false;
    if (button.textContent === '读取中…') button.textContent = previousText;
  }
}
async function loadResearchState(taskId) {
  try {
    const state = await req('/tasks/' + taskId + '/research');
    if (selected !== taskId) return;
    researchState = state;
    renderResearchState();
  } catch (error) {
    if (selected !== taskId) return;
    $('researchControlPanel').innerHTML = '<div class="panelhead">研究控制与人工干预</div><div class="load-error">研究状态读取失败：' + esc(error.message) + '</div>';
  }
}

function renderResearchState() {
  const root=$('researchControlPanel');
  if(!root||!detail)return;
  const state=researchState||{},control=state.control||{},coverage=control.coverage||{};
  const dimensions=Array.isArray(coverage.dimensions)?coverage.dimensions:[];
  const assessments=Array.isArray(state.quality_assessments)?state.quality_assessments:[];
  const latest=assessments.length?assessments[assessments.length-1]:null;
  const recovery=Array.isArray(state.recovery_decisions)?state.recovery_decisions.slice(-6).reverse():[];
  const pending=state.pending_intervention||null;
  const labels={
    continue_research:'继续研究',change_research_direction:'调整研究方向',
    add_research_requirements:'补充研究要求',continue_repair:'继续修复',
    approve_degraded_delivery:'批准降级交付',terminate:'终止任务'
  };
  const statusLabels={covered:'已覆盖',partial:'部分覆盖',uncovered:'未覆盖'};
  const dimensionHtml=dimensions.length
    ?'<div class="research-dimensions">'+dimensions.map(item=>
      '<div class="research-dimension"><div><b>'+esc(item.title||item.id)+'</b><small>'+esc(item.priority||'normal')+' · '+esc(statusLabels[item.status]||item.status||'待评估')+'</small></div>'+
      '<span>'+esc(item.valid_source_count??0)+' / '+esc(item.min_sources??1)+' 个有效来源</span></div>').join('')+'</div>'
    :'<div class="empty">研究覆盖矩阵尚未建立。</div>';
  const qualityHtml=latest
    ?'<div class="research-quality"><b>最近一次质量评估</b><span class="'+(latest.passed?'quality-pass':'quality-fail')+'">'+(latest.passed?'通过':'未通过')+'</span>'+
      '<strong>'+Number(latest.total_score||0).toFixed(1)+' 分</strong><small>评审状态：'+esc(latest.reviewer?.status||'未记录')+'</small>'+
      '<div class="research-score-grid">'+Object.entries(latest.dimension_scores||{}).map(([key,value])=>
        '<span>'+esc(({goal_coverage:'目标覆盖',evidence_sufficiency:'证据充分性',source_reliability:'来源可靠性',conclusion_consistency:'结论一致性',report_completeness:'报告完整性'})[key]||key)+
        ' <b>'+Number(value||0).toFixed(1)+'</b></span>').join('')+'</div></div>'
    :'<div class="empty">尚无五维质量评估记录。</div>';
  const recoveryHtml=recovery.length
    ?'<div class="research-recovery-list">'+recovery.map(item=>
      '<div><b>'+esc(item.category||'恢复')+' · '+esc(item.action||'')+'</b><small>'+esc(item.confidence||'')+' · '+esc(time(item.created_at))+'</small>'+
      '<p>'+esc(compact(item.details?.reason||item.details?.summary||'',220))+'</p></div>').join('')+'</div>'
    :'<div class="empty">尚无恢复决策记录。</div>';
  const pendingHtml=pending
    ?'<div class="human-pending"><b>任务正在等待人工决策</b><p>'+esc(pending.request?.message||pending.request?.summary||'系统请求人工介入。')+'</p>'+
      '<small>类型：'+esc(pending.category)+' · 创建：'+esc(time(pending.created_at))+'</small>'+
      '<form id="humanDecisionForm" class="human-decision-form"><label>决策动作<select id="humanDecisionAction">'+
      (Array.isArray(pending.allowed_actions)?pending.allowed_actions:(Array.isArray(pending.request?.choices)?pending.request.choices:[])).filter(key=>labels[key]).map(key=>'<option value="'+esc(key)+'">'+labels[key]+'</option>').join('')+
      '</select></label><label>处理说明 / 方向 / 新增要求<textarea id="humanDecisionInput" rows="4" placeholder="填写决策理由；调整方向时填写新的研究方向；补充要求时填写 JSON。"></textarea></label>'+
      '<label id="humanDecisionConfirmWrap" class="human-confirm" hidden><input type="checkbox" id="humanDecisionConfirm"> 我已审阅报告，并明确批准降级交付</label>'+
      '<button class="primary" type="submit">提交人工决策</button></form></div>'
    :'<div class="empty">'+(detail.status==='waiting_for_human'?'等待人工决策记录读取完成。':'当前没有待处理的人工决策。')+'</div>';
  root.innerHTML='<div class="panelhead">研究控制与人工干预</div>'+
    '<div class="research-overview"><span>覆盖率 <b>'+ (coverage.coverage_ratio==null?'—':Math.round(Number(coverage.coverage_ratio)*100)+'%')+'</b></span>'+
    '<span>关键缺口 <b>'+esc((coverage.critical_gaps||[]).length)+'</b></span>'+
    '<span>停滞周期 <b>'+esc(coverage.stagnation_count??control.stagnation_count??0)+'</b></span>'+
    '<span>研究阶段 <b>'+esc(control.phase||'planning')+'</b></span></div>'+
    '<div class="research-section"><b>证据覆盖矩阵</b>'+dimensionHtml+'</div>'+
    '<div class="research-section">'+qualityHtml+'</div>'+
    '<div class="research-section"><b>恢复决策记录</b>'+recoveryHtml+'</div>'+
    pendingHtml+
    (Array.isArray(state.human_decisions)&&state.human_decisions.length?
      '<details class="human-history"><summary>已提交人工决策（'+state.human_decisions.length+'）</summary>'+
      state.human_decisions.slice(-5).reverse().map(item=>'<p><b>'+esc(item.payload?.action||'')+'</b> · '+esc(time(item.timestamp))+
      '<br>'+esc(compact(item.payload?.payload?.reason||item.payload?.payload?.direction||'',180))+'</p>').join('')+'</details>':'');
  const action=$('humanDecisionAction');
  const updateForm=()=>{
    if(!action)return;
    const kind=action.value,input=$('humanDecisionInput'),confirm=$('humanDecisionConfirmWrap');
    if(kind==='add_research_requirements')input.placeholder='输入 JSON，例如：{"dimensions":[{"id":"security","title":"安全边界","questions":["有哪些安全边界？"],"expected_evidence":["官方安全文档"],"priority":"critical","min_sources":2}]}';
    else if(kind==='change_research_direction')input.placeholder='写明新的研究方向、优先级和需要解决的问题。';
    else if(kind==='approve_degraded_delivery')input.placeholder='说明已审阅报告并批准降级交付的原因。';
    else input.placeholder='填写本次人工决策的理由或补充说明（可选）。';
    confirm.hidden=kind!=='approve_degraded_delivery';
  };
  if(action)action.addEventListener('change',updateForm);
  updateForm();
  $('humanDecisionForm')?.addEventListener('submit',async event=>{
    event.preventDefault();
    const kind=$('humanDecisionAction').value,raw=$('humanDecisionInput').value.trim();
    const body={intervention_id:pending.intervention_id,action:kind,reason:raw};
    if(kind==='change_research_direction')body.direction=raw;
    if(kind==='add_research_requirements'){
      try{body.requirements=JSON.parse(raw);}catch(error){alert('研究要求必须是合法 JSON：'+error.message);return;}
      body.reason='人工补充研究要求';
    }
    if(kind==='approve_degraded_delivery')body.confirm=$('humanDecisionConfirm').checked;
    const submit=event.currentTarget.querySelector('button[type="submit"]');
    submit.disabled=true;submit.textContent='正在提交…';
    try{
      await req('/tasks/'+detail.task_id+'/human-intervention/resolve',{method:'POST',body:JSON.stringify(body)});
      detail=await req('/tasks/'+detail.task_id);renderTaskSummary();await loadResearchState(detail.task_id);renderTasks();
    }catch(error){alert('人工决策提交失败：'+error.message);submit.disabled=false;submit.textContent='提交人工决策';}
  });
}

function renderQuality() {
  const root=$('qualityPanel'),event=latestValidation();
  if(!event){root.innerHTML='<div class="panelhead">交付质量</div><div class="empty">尚无交付校验记录；报告生成后将在这里显示校验结论。</div>';return;}
  const payload=event.payload||{},role=getAgent(detail?.agent_type),maxRevisions=Number(role?.max_revisions??0);
  const revision=Number(payload.revision||0),exhausted=detail?.error_code==='DELIVERY_VALIDATION_EXHAUSTED';
  const errors=Array.isArray(payload.errors)?payload.errors:String(payload.errors||'').split(';').filter(Boolean);
  root.innerHTML='<div class="panelhead">交付质量 <span class="quality-'+(payload.valid?'pass':'fail')+'">'+
    (payload.valid?'校验通过':'校验未通过')+'</span></div><div class="quality-metrics"><span>已修订轮次 <b>'+
    revision+' / '+maxRevisions+'</b></span><span>证据覆盖率 <b>'+
    (payload.evidence_coverage==null?'未记录':Math.round(Number(payload.evidence_coverage)*100)+'%')+
    '</b></span><span>核验来源 <b>'+esc(payload.source_count??'—')+'</b></span></div>'+
    (exhausted?'<p class="quality-exhausted">修订已耗尽，任务终止。</p>':'')+
    (!payload.valid?'<div class="quality-issues"><b>未通过项</b><ul>'+errors.map(error=>'<li>'+esc(error)+'</li>').join('')+'</ul></div>':'<p class="muted">最近一次交付校验已通过。</p>');
}

function renderInstanceDiagnostics() {
  const root=$('instanceDiagnostics');
  if(!root||!detail)return;
  const wasOpen = root.querySelector('details.instance-diagnostics')?.open ?? false;
  const count=type=>events.filter(e=>e.event_type===type).length;
  const recovery=events.filter(e=>['checkpoint_resume','worker_recovery_scheduled','checkpoint_already_complete'].includes(e.event_type)).length;
  const errors=events.filter(e=>['execution_exception','model_call_failed','tool_call_failed','research_page_fetch_failed','worker_recovery_scheduled','retry_scheduled','task_failed','research_plan_archive_failed'].includes(e.event_type)).slice(-8).reverse();
  const attempts=events.filter(e=>e.event_type==='task_started').slice(-8).reverse();
  const heartbeat=detail.heartbeat_at?new Date(Number(detail.heartbeat_at)*1000).toLocaleString():'当前无 Worker 心跳';
  const next=detail.next_run_at>0?new Date(Number(detail.next_run_at)*1000).toLocaleString():'—';
  root.innerHTML='<details class="instance-diagnostics" '+(wasOpen?'open':'')+'><summary>运行实例与故障诊断</summary><div class="instance-grid">'+
    '<div><small>状态</small><b>'+esc(detail.status)+'</b></div><div><small>Worker</small><b>'+esc(detail.worker_id||'尚未分配')+'</b></div>'+
    '<div><small>最近心跳</small><b>'+esc(heartbeat)+'</b></div><div><small>Checkpoint</small><b>'+(detail.resume_mode?'等待恢复 / 请求复用':'常规执行')+'</b></div>'+
    '<div><small>下次运行</small><b>'+esc(next)+'</b></div><div><small>恢复事件</small><b>'+recovery+' 次</b></div></div>'+
    '<div class="instance-metrics"><span>模型 '+count('model_call_started')+' 次 / 失败 '+count('model_call_failed')+
    '</span><span>工具 '+count('tool_call_started')+' 次 / 失败 '+count('tool_call_failed')+'</span></div>'+
    (attempts.length?'<div class="instance-subtitle">Worker 执行尝试</div><ol class="instance-attempts">'+attempts.map(e=>
      '<li><b>第 '+esc(e.payload?.attempt??'—')+' 次</b><span>'+esc(e.payload?.worker_id||'未记录')+'</span><time>'+time(e.timestamp)+'</time></li>').join('')+'</ol>':'')+
    (errors.length?'<div class="instance-subtitle">异常与恢复记录</div><div class="instance-errors">'+errors.map(e=>{
      const p=e.payload||{},m=p.error||p.reason||p.message||p.error_type||p.pending_nodes||e.event_type;
      return '<div class="instance-error"><b>'+esc(eventNames[e.event_type]||e.event_type)+'</b><time>'+time(e.timestamp)+'</time><p>'+
        esc(compact(Array.isArray(m)?m.join(', '):m,320))+'</p></div>';}).join('')+'</div>':'<p class="instance-no-errors">当前没有异常或恢复事件。</p>')+'</details>';
}

function renderArtifacts() {
  $('artifacts').innerHTML = artifacts.map(file =>
    '<button type="button" class="file ' + (activeFile === file.path ? 'active' : '') +
    '" data-file="' + esc(file.path) + '">▤　' + esc(file.path) +
    '　<small>' + Math.round(file.size_bytes / 1024 * 10) / 10 + ' KB</small></button>'
  ).join('') || '<div class="empty">Agent 尚未保存文件</div>';
  document.querySelectorAll('[data-file]').forEach(element => element.addEventListener('click', () => openFile(element.dataset.file)));
}

function normalizedPlan(plan = {}) {
  return {
    title: String(plan.title || '研究执行计划'),
    objective: String(plan.objective || ''),
    steps: (Array.isArray(plan.steps) ? plan.steps : []).map((step, index) => ({
      id: String(step.id || 'step-' + (index + 1)),
      title: String(step.title || ''),
      objective: String(step.objective || ''),
      expected_output: String(step.expected_output || ''),
      expected_output_path: String(step.expected_output_path || ''),
      phase: String(step.phase || ''),
      dependencies: Array.isArray(step.dependencies) ? step.dependencies.map(String) : []
    }))
  };
}

function planSnapshot() {
  if (planSnapshotTaskId === selected && planSnapshotData) {
    const versions = (planSnapshotData.history || []).map((item, index) => {
      const plan = normalizedPlan(item.plan || {});
      const payload = {
        original_plan_text: item.original_plan_text || '',
        change_type: item.change_type || (index ? 'replan' : 'initial')
      };
      return {
        version: Number(item.version || index + 1), plan, signature: JSON.stringify(plan),
        event: { timestamp: item.timestamp, payload }, changeType: payload.change_type,
        trigger: item.trigger || '', newInformation: item.new_information || '',
        problemAnalysis: item.problem_analysis || '', decision: item.decision || '',
        changes: item.changes || '', planDiff: item.plan_diff || {}
      };
    });
    if (!versions.length && planSnapshotData.current_plan) {
      const plan = normalizedPlan(planSnapshotData.current_plan);
      versions.push({
        version: Number(planSnapshotData.current_version || 1), plan, signature: JSON.stringify(plan),
        event: { timestamp: planSnapshotData.last_updated_at, payload: {} },
        changeType: 'initial', trigger: '', newInformation: '', problemAnalysis: '',
        decision: '', changes: '', planDiff: {}
      });
    }
    const latest = versions.length ? versions[versions.length - 1] : null;
    const finalizedEvent = planSnapshotData.finalized
      ? { event_id: Number.MAX_SAFE_INTEGER, timestamp: planSnapshotData.finalized_at } : null;
    return { versions, latest, finalizedEvent, progress: planSnapshotData.progress || {} };
  }
  const versions=[];
  for(const event of events){
    if(event.event_type!=='research_plan_updated')continue;
    const p=event.payload||{},plan=normalizedPlan(p.plan||{});
    if(!plan.steps.length)continue;
    const signature=JSON.stringify(plan);
    if(versions.length&&versions[versions.length-1].signature===signature)continue;
    versions.push({version:versions.length+1,plan,signature,event,changeType:p.change_type||(versions.length?'replan':'initial'),
      trigger:p.trigger||p.change_reason||'',newInformation:p.new_information||'',problemAnalysis:p.problem_analysis||'',
      decision:p.decision||'',changes:p.changes||p.change_reason||'',planDiff:p.plan_diff||{}});
  }
  const latest=versions.length?versions[versions.length-1]:null;
  const finalizedEvent=latest?[...events].reverse().find(e=>e.event_type==='research_plan_finalized'&&e.event_id>latest.event.event_id)||null:null;
  const progress={};
  for(const e of events){if(e.event_type!=='research_plan_step_status')continue;const p=e.payload||{};if(p.step_id)progress[p.step_id]={status:p.status||'pending',note:p.note||'',verified_output:p.verified_output||null};}
  return {versions,latest,finalizedEvent,progress};
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
  const {versions,latest,finalizedEvent,progress}=planSnapshot();planVersions=versions;
  const root=$('planPanel');if(!latest){root.innerHTML='<div class="empty plan-empty">Agent 尚未提交整体研究计划。</div>';return;}
  const counts={pending:0,in_progress:0,awaiting_verification:0,retrying:0,blocked:0,paused:0,completed:0,skipped:0,failed:0};
  const steps=latest.plan.steps.map(step=>{const status=progress[step.id]?.status||'pending';counts[status]=(counts[status]||0)+1;return {...step,status,note:progress[step.id]?.note||''};});
  const total=steps.length,done=counts.completed+counts.skipped,pct=total?Math.round(done/total*100):0;
  const labels={pending:'待执行',in_progress:'进行中',awaiting_verification:'待产物核验',retrying:'重试中',blocked:'受阻',paused:'已暂停',completed:'已完成',skipped:'已跳过',failed:'失败'};
  root.innerHTML='<div class="plan-top"><div><strong>'+esc(latest.plan.title)+'</strong><p>'+esc(latest.plan.objective)+'</p></div><span class="plan-version">'+
    (finalizedEvent?'最终计划已归档':'规划执行中')+' · v'+latest.version+'</span></div>'+
    '<div class="plan-progress"><span>执行进度 '+done+'/'+total+'（'+pct+'%）</span><div><i style="width:'+pct+'%"></i></div></div>'+
    '<div class="plan-progress-counts"><span>已完成 '+counts.completed+'</span><span>进行中 '+counts.in_progress+'</span><span>待执行 '+counts.pending+
    '</span><span>待核验 '+counts.awaiting_verification+'</span><span>重试中 '+counts.retrying+'</span><span>受阻 '+counts.blocked+'</span><span>已暂停 '+counts.paused+'</span><span>已跳过 '+counts.skipped+'</span><span>失败 '+counts.failed+'</span></div>'+
    '<div class="plan-section-label">当前整体执行计划</div><ol class="plan-steps">'+steps.map((s,i)=>
      '<li class="plan-step '+esc(s.status)+'"><span class="plan-index">'+(i+1)+'</span><div><strong>'+esc(s.title)+'</strong>'+
      (s.objective?'<p>'+esc(s.objective)+'</p>':'')+(s.expected_output?'<small>预期成果：'+esc(s.expected_output)+'</small>':'')+
      (s.expected_output_path?'<small>产物路径：<code>'+esc(s.expected_output_path)+'</code></small>':'')+
      (s.note?'<small class="plan-step-note">'+esc(s.note)+'</small>':'')+'</div><span class="plan-state">'+labels[s.status]+'</span></li>').join('')+
    '</ol><p class="plan-archive-note">步骤进度独立记录，不产生新的策略版本。</p>'+
    (finalizedEvent?'<p class="plan-archive-note">完整计划及进度归档至 <code>notes/agent_execution_plan.json</code></p>':'')+
    '<details class="plan-history" '+(versions.length>1?'':'hidden')+'><summary>查看规划演进历史（'+versions.length+' 个有效版本）</summary>'+
    versions.map(planHistoryEntry).join('')+'</details>'+
    (latest.event.payload.original_plan_text?'<details class="plan-version-detail"><summary>查看 Agent 提交的原始计划</summary><pre>'+esc(latest.event.payload.original_plan_text)+'</pre></details>':'');
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
  let status = traceStatus(item);
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
    const encoded=String(path).split('/').map(encodeURIComponent).join('/');
    const response=await fetch('/tasks/'+encodeURIComponent(selected)+'/artifacts/'+encoded);
    if(!response.ok)throw Error('读取快照 / 文件失败：HTTP '+response.status);
    fileText=await response.text();activeFile=path;renderArtifactPreview();
    if(rerender)renderArtifacts();
  }catch(error){$('preview').innerHTML='<div class="load-error">'+esc(error.message)+'</div>';}
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
$('refresh').onclick = () => refresh(true);
$('search').oninput = renderTasks;
$('filter').onchange = renderTasks;
$('sort').onchange = renderTasks;
$('agentType').onchange = renderAgentDefaults;
$('newTask').onclick = async () => { await loadAgents(); $('createDialog').showModal(); };
$('closeDialog').onclick = () => $('createDialog').close();
$('closeBudgetDialog').onclick = () => $('budgetDialog').close();

$('createForm').onsubmit = async event => {
  event.preventDefault();
  const form=new FormData(event.target);
  const body={goal:form.get('goal'),agent_type:form.get('agent_type'),
    max_attempts:Number(form.get('max_attempts')),budget_enabled:form.get('budget_enabled')==='on'};
  for(const key of ['max_steps','max_runtime_seconds','max_total_tokens']){
    const value=String(form.get(key)||'').trim();if(value)body[key]=Number(value);
  }
  try{
    const task=await req('/tasks',{method:'POST',body:JSON.stringify(body)});
    $('createDialog').close();event.target.reset();renderAgentDefaults();
    selected=task.task_id;await load(selected);await refresh();
  }catch(error){alert(error.message);}
};

$('budgetForm').onsubmit = async event => {
  event.preventDefault();if(!detail)return;
  const form=new FormData(event.target);
  const body={reason:form.get('reason'),additional_steps:Number(form.get('additional_steps')||0),
    additional_runtime_seconds:Number(form.get('additional_runtime_seconds')||0),additional_tokens:Number(form.get('additional_tokens')||0)};
  try{
    const result=await req('/tasks/'+detail.task_id+'/budget/approve',{method:'POST',body:JSON.stringify(body)});
    $('budgetDialog').close();event.target.reset();await refresh(true);
    setStreamStatus(result.resumed_from_checkpoint?'额度已叠加、可从断点续跑':'额度已叠加',false);
  }catch(error){alert(error.message);}
};

['pause','resume','cancel','retry'].forEach(name=>{
  $(name).onclick=()=>{
    if(name==='cancel'||name==='retry'){
      if(!confirm(name==='cancel'?'确认取消任务？':'重新执行失败任务并尝试恢复 Checkpoint？'))return;
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

loadAgents();
refresh();
let listRefreshTimer=setInterval(()=>refresh(),3000);
document.addEventListener('visibilitychange',()=>{
  if(document.visibilityState==='hidden'){
    if(listRefreshTimer)clearInterval(listRefreshTimer);
    if(eventPollTimer)clearInterval(eventPollTimer);
  }else{
    refresh();listRefreshTimer=setInterval(()=>refresh(),3000);
    if(selected&&eventStreamMode==='polling')startEventPolling(selected);
  }
});
