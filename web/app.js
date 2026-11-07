/* No dependencies. Runtime edits are sent only to the localhost editor API. */
(() => {
  'use strict';
  const $ = (selector) => document.querySelector(selector);
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const idOf = (skill) => String(skill.id || skill.path || skill.name);
  const textOf = (value) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const sourceNames = {personal:'个人技能',system:'系统技能',plugin:'插件技能'};
  const state = {inventory:null,revision:0,skills:[],findings:[],relationships:[],comparison:new Set(),stale:new Set(),selected:null,source:'personal',query:'',tab:'overview',live:false,config:null,drafts:new Map(),draftBaselines:new Map(),details:new Map(),intents:new Map(),intentDrafts:new Map(),previews:new Map(),busy:false,refreshing:false};
  let toastTimer;
  const atlas = window.SkillAtlas.create({
    viewport: $('#skill-atlas'), world: $('#atlas-world'),
    onSelect(id) { state.tab = 'overview'; selectSkill(id); },
    onViewportChange({scale}) { $('#atlas-zoom-value').textContent = `${Math.round(scale * 100)}%`; },
  });
  const caseWorkspace = window.SkillCases.create({
    container: $('#case-workspace'),
    getSkills: () => state.skills,
    notify: (message, error = false) => toast(message, error),
    onUseCandidate(id, description, baselineSha) {
      const skill = skillById(id);
      if (!skill || !isEditable(skill)) { toast('此候选没有对应到当前可编辑的个人技能。',true); return false; }
      if (!storeDraft(skill,description,baselineSha)) return false;
      state.tab = 'trigger';
      selectSkill(id);
      setTab('trigger');
      toast('候选已放入编辑草稿。核对差异后再选择应用。');
      return true;
    },
  });

  function closeInspector() {
    $('#skill-drawer').hidden = true;
    state.selected = null;
    renderList();
    renderComparison();
  }

  function closeMaintenance() {
    $('#maintenance-drawer').hidden = true;
    $('#open-maintenance').setAttribute('aria-expanded','false');
  }

  function openMaintenance(view = 'cases', {skillIds} = {}) {
    $('#skill-drawer').hidden = true;
    $('#maintenance-drawer').hidden = false;
    $('#open-maintenance').setAttribute('aria-expanded','true');
    document.querySelectorAll('[data-maintenance-view]').forEach((node) => { node.hidden = node.dataset.maintenanceView !== view; });
    document.querySelectorAll('[data-maintenance]').forEach((button) => {
      const active = button.dataset.maintenance === view;
      button.classList.toggle('active',active);
      button.setAttribute('aria-pressed',String(active));
    });
    $('#analyze-button').hidden = view === 'cases';
    if (view === 'cases') caseWorkspace.open({skillIds});
  }

  function toast(message, error = false) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.toggle('error', error);
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, error ? 8000 : 4300);
  }

  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method,headers:body === undefined ? {} : {'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body),cache:'no-store'});
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('编辑器接口未返回 JSON。请使用 python3 -m skill_editor 启动项目。'); }
    if (!response.ok || result.error) throw new Error(result.error || `请求失败 (${response.status})`);
    return result;
  }

  const skillById = (id) => state.skills.find((skill) => idOf(skill) === String(id));
  const selectedSkill = () => skillById(state.selected);
  const currentDescription = (skill) => state.drafts.has(idOf(skill)) ? state.drafts.get(idOf(skill)) : (skill.description || '');
  const isReadable = (skill) => Boolean(skill && (skill.fileStatus === undefined || skill.fileStatus === 'present'));
  const isEditable = (skill) => Boolean(isReadable(skill) && skill.editable && skill.source === 'personal');
  const nameFor = (id) => skillById(id)?.name || id;
  const sessionLabel = (skill) => skill.sessionAvailable === true ? '原会话目录可见' : skill.sessionAvailable === false ? '原会话目录未显示' : '会话可用状态未核验';
  const fileLabel = (skill) => skill.fileStatus === 'missing' ? '文件缺失' : skill.fileStatus === 'present' ? '文件可读' : skill.fileStatus === 'unsupported' ? '文件格式不支持' : skill.fileStatus === 'unavailable-or-unsupported' ? '文件不可读或格式不支持' : skill.fileStatus === undefined ? '磁盘快照记录' : '文件不可读或状态待核验';

  function storeDraft(skill, description, baseline) {
    const id = idOf(skill);
    if (arguments.length >= 3) {
      if (typeof baseline !== 'string' || !/^[0-9a-f]{64}$/.test(baseline)) {
        toast('该 AI 建议没有可核对的源文件版本，请重新分析后再放入草稿。',true);
        return false;
      }
      // An AI proposal carries its own source version, even when replacing an
      // existing draft. It must never inherit a newer draft's baseline.
      state.draftBaselines.set(id,baseline);
      const currentSha = state.details.get(id)?.sha || skill.sha256;
      if (currentSha && baseline !== currentSha) state.stale.add(id);
      else if (currentSha === baseline) state.stale.delete(id);
    } else if (!state.draftBaselines.has(id)) {
      const sha = state.details.get(id)?.sha || skill.sha256;
      if (sha) state.draftBaselines.set(id,sha);
    }
    state.drafts.set(id,description);
    state.previews.delete(id);
    renderComparison();
    return true;
  }

  function heuristicFindings(skills) {
    const findings = [];
    const named = (names) => skills.filter((skill) => names.includes(skill.name));
    const generalReviewers = named(['paper-reviewer','reliable-paper-reviewer','iclr-reviewer']);
    if (generalReviewers.length > 1) findings.push({id:'local-review-routing',type:'routing-overlap',severity:'review',skillIds:generalReviewers.map(idOf),message:'通用审稿入口覆盖相近的用户请求',evidence:generalReviewers.map((skill) => `${skill.name}: ${skill.description.slice(0, 175)}…`).join('\n'),suggestion:'比较通用评审、校准判断与指定 venue 的入口，明确主入口及专门技能的适用边界。这里只提示相近描述，尚未证明行为冲突。',method:'heuristic'});
    const writing = named(['paper-reviewer','paper-structure-check','scientific-writing','empirical-ml-paper-writing']);
    if (writing.length > 1) findings.push({id:'local-writing-routing',type:'scope-boundary',severity:'review',skillIds:writing.map(idOf),message:'结构诊断、论文写作与评审之间需要保留边界',evidence:writing.map((skill) => `${skill.name}: ${skill.description.slice(0, 150)}…`).join('\n'),suggestion:'以用户要得到的结果区分触发条件：评审结论、全文重排，或依据材料撰写正文。',method:'heuristic'});
    const duplicates = new Map();
    skills.forEach((skill) => { const list = duplicates.get(skill.name) || []; list.push(skill); duplicates.set(skill.name,list); });
    duplicates.forEach((list,name) => { if (list.length > 1) findings.push({id:`duplicate-${name}`,type:'duplicate-name',severity:'review',skillIds:list.map(idOf),message:`存在同名技能：${name}`,evidence:list.map((skill) => skill.path).join('\n'),suggestion:'确认来源、版本与当前会话可见性后再决定是否整理；同名本身不证明重复执行。',method:'heuristic'}); });
    return findings;
  }

  async function loadInventory(providedInventory) {
    $('#refresh-button').disabled = true;
    let inventory = providedInventory;
    state.live = false;
    if (inventory) state.live = true;
    if (!inventory && (location.protocol === 'http:' || location.protocol === 'https:')) {
      try { inventory = await api('/api/inventory'); state.live = true; } catch (_) { /* A static preview can use the saved snapshot. */ }
    }
    if (!inventory) inventory = window.SKILL_EDITOR_INVENTORY;
    if (!inventory && location.protocol !== 'file:') {
      try { const response = await fetch('../data/inventory.json'); if (response.ok) inventory = await response.json(); } catch (_) { /* Show a useful missing-data state below. */ }
    }
    $('#refresh-button').disabled = false;
    if (!inventory?.skills) {
      $('#skill-list').innerHTML = '<div class="empty-state">没有找到技能快照。请从项目目录启动 <code>python3 -m skill_editor</code>，或生成 web/inventory.js。</div>';
      $('#connection-status').textContent = '目录未载入';
      toast('未载入真实技能目录。未使用演示数据。', true);
      return;
    }
    state.inventory = inventory;
    state.revision += 1;
    state.skills = inventory.skills;
    state.relationships = Array.isArray(inventory.relationships) ? inventory.relationships : [];
    state.details.clear();
    state.previews.clear();
    state.drafts.forEach((draft,id) => { const skill = skillById(id); const baseline = state.draftBaselines.get(id); if (skill && baseline && skill.sha256 && baseline !== skill.sha256) state.stale.add(id); });
    state.comparison.forEach((id) => { if (!skillById(id)) state.comparison.delete(id); });
    state.skills.forEach((skill) => { if (typeof skill.intent === 'string') state.intents.set(idOf(skill),skill.intent); });
    const severityWeight = {critical:5,high:4,error:4,warning:3,medium:2,review:1,low:0,info:0};
    state.findings = [...(Array.isArray(inventory.findings) ? inventory.findings : heuristicFindings(state.skills))].sort((left,right) => ((severityWeight[right.severity] || 0) + (right.type === 'instruction-conflict' ? 5 : 0)) - ((severityWeight[left.severity] || 0) + (left.type === 'instruction-conflict' ? 5 : 0)));
    state.config = null;
    if (state.live) { try { state.config = await api('/api/config'); } catch (_) { /* The core editor remains usable. */ } }
    renderOverview();
    renderList();
    renderComparison();
    const initial = skillById(state.selected);
    if (initial) selectSkill(idOf(initial), {open:!$('#skill-drawer').hidden});
  }

  async function refreshInventory() {
    if (state.refreshing || state.busy) return;
    if (!state.live) { toast('静态快照不能重新扫描磁盘。请连接本地编辑器后刷新。'); return; }
    state.refreshing = true;
    const button = $('#refresh-button');
    button.disabled = true;
    button.setAttribute('aria-busy','true');
    try {
      const result = await api('/api/inventory/refresh','POST',{});
      await loadInventory(result.inventory || result);
      toast('磁盘盘点已更新；会话可用目录仍保留原快照口径。');
    } catch (error) { toast(error.message,true); }
    finally { state.refreshing = false; button.disabled = false; button.removeAttribute('aria-busy'); if (state.inventory) renderInventoryUpdate(); }
  }

  function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? '未记录时间' : date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  }

  function renderInventoryUpdate() {
    const inventory = state.inventory;
    const delta = inventory.delta || {};
    const kinds = [['added','新增'],['missing','缺失'],['changed','内容变化'],['restored','恢复'],['unverified','待核验']];
    const warnings = Array.isArray(inventory.scanWarnings) ? inventory.scanWarnings : [];
    const changes = kinds.filter(([key]) => Array.isArray(delta[key]) && delta[key].length);
    const capturedAt = inventory.sessionCatalog?.capturedAt || inventory.generatedAt;
    const refresh = inventory.refresh;
    const snapshotTime = refresh?.at || inventory.generatedAt;
    $('#inventory-update').innerHTML = `<div class="inventory-update-heading"><strong>${refresh ? (refresh.complete === false ? '磁盘盘点有未完成项' : '磁盘盘点已更新') : '已保存的磁盘盘点'}</strong><span>${escapeHTML(formatDate(snapshotTime))}</span><button id="rescan-inventory" class="secondary-button" ${state.refreshing ? 'disabled' : ''}>重新扫描</button></div><p>会话目录来自 ${escapeHTML(formatDate(capturedAt))} 的独立快照；磁盘文件和缓存不会自动计为当前可用。</p>${changes.length ? `<div class="inventory-delta">${changes.map(([key,label]) => `<details><summary>${label} <strong>${delta[key].length}</strong></summary><ul>${delta[key].map((item) => `<li>${item.id && skillById(item.id) ? `<button data-skill-id="${escapeHTML(item.id)}">${escapeHTML(item.name || nameFor(item.id))}</button>` : `<strong>${escapeHTML(item.name || item.id || item.path || textOf(item))}</strong>`}${item.path ? `<code>${escapeHTML(item.path)}</code>` : ''}</li>`).join('')}</ul></details>`).join('')}</div>` : `<p class="inventory-no-change">${refresh ? '本次扫描未记录新增、缺失或内容变化。' : '点击重新扫描，可发现新增文件并保留旧快照。'}</p>`}${warnings.length ? `<details class="scan-warnings" open><summary>扫描提示 · ${warnings.length}</summary><ul>${warnings.map((warning) => `<li>${escapeHTML(warning.message || textOf(warning))}${warning.path ? `<code>${escapeHTML(warning.path)}</code>` : ''}</li>`).join('')}</ul><p>未能完整检查的路径保持待核验，不据此认定文件已删除。</p></details>` : ''}`;
    $('#rescan-inventory').addEventListener('click',refreshInventory);
    if (inventory.counts?.diskCountsComplete === false || inventory.counts?.physicalCountIsLowerBound) $('#inventory-update').insertAdjacentHTML('beforeend','<p class="edge-caution">部分路径尚未完整读取，磁盘数量仅为已确认文件的下限。</p>');
    const snapshotPath = refresh?.snapshotPath || refresh?.previousSnapshotPath;
    if (snapshotPath) $('#inventory-update').insertAdjacentHTML('beforeend',`<details class="previous-snapshot"><summary>旧快照已保留</summary><code>${escapeHTML(snapshotPath)}</code></details>`);
  }

  function renderOverview() {
    const counts = state.inventory.counts || {};
    const available = counts.sessionAvailable ?? state.skills.filter((skill) => skill.sessionAvailable).length;
    const personal = counts.personal ?? state.skills.filter((skill) => skill.source === 'personal').length;
    $('#stat-available').textContent = available;
    $('#stat-personal').textContent = counts.diskCountsComplete === false ? `≥ ${personal}` : personal;
    $('#stat-findings').textContent = state.findings.length;
    const hasSourceReview = state.findings.some((finding) => finding.method === 'source-review');
    $('#stat-findings-caption').textContent = hasSourceReview ? '维护提示 · 含源码审阅' : '维护提示 · 规则检查';
    $('#findings-method').textContent = hasSourceReview ? '源码审阅 + 规则检查' : '规则检查';
    $('#catalog-count').textContent = state.skills.length;
    $('#connection-status').innerHTML = `<i></i>${state.live ? '本地已连接' : '快照预览'}`;
    $('#snapshot-time').textContent = `磁盘 · ${formatDate(state.inventory.refresh?.at || state.inventory.generatedAt)}`;
    $('#inventory-scope').textContent = counts.physicalSkillFiles !== undefined ? `盘内 ${counts.physicalSkillFiles} 份 SKILL.md，含缓存与未显示条目` : `${state.skills.length} 个目录条目`;
    const note = $('#mode-note');
    if (!state.live) {
      note.hidden = false;
      note.innerHTML = '当前为真实快照预览，可浏览与编辑草稿。要保存文件或调用 AI，请在项目目录运行 <code>python3 -m skill_editor</code>，再打开终端显示的本地地址。';
    } else if (!state.config?.aiConfigured) {
      note.hidden = false;
      note.textContent = `文件编辑器已连接。AI 分析需在服务端配置 OPENROUTER_API_KEY 与 OPENROUTER_MODEL；当前提示来自${hasSourceReview ? '源码审阅与规则检查' : '规则检查'}。`;
    } else { note.hidden = true; }
    renderInventoryUpdate();
    renderFindings();
  }

  function renderList() {
    const query = state.query.toLocaleLowerCase().trim();
    const visible = state.skills.filter((skill) => {
      const meta = window.SkillAtlas.meta(skill);
      return (state.source === 'all' || skill.source === state.source)
        && (!query || `${meta.title} ${meta.subtitle} ${meta.categoryLabel} ${skill.description || ''}`.toLocaleLowerCase().includes(query));
    });
    const groups = ['personal','system','plugin'];
    $('#skill-list').innerHTML = visible.length ? groups.map((source) => {
      const group = visible.filter((skill) => skill.source === source);
      if (!group.length) return '';
      return `<div class="skill-list-group">${sourceNames[source]} <span>· ${group.length}</span></div>${group.map((skill) => `<button class="skill-item ${idOf(skill) === state.selected ? 'selected' : ''} ${skill.fileStatus === 'missing' ? 'missing-skill' : ''}" data-skill-id="${escapeHTML(idOf(skill))}" aria-pressed="${idOf(skill) === state.selected}" title="${escapeHTML(`${skill.name} · ${sessionLabel(skill)} · ${fileLabel(skill)}`)}"><span class="skill-glyph" aria-hidden="true">${source === 'personal' ? '◇' : source === 'system' ? '⌘' : '▧'}</span><span class="skill-item-text">${escapeHTML(skill.name)}${skill.fileStatus === 'missing' ? '<small>文件缺失</small>' : ''}</span></button>`).join('')}`;
    }).join('') : '<div class="empty-state">没有匹配的技能。试试缩短搜索词，或切换来源。</div>';
    atlas.setData({skills:state.skills,relationships:state.relationships,selectedId:state.selected,query:state.query,source:state.source});
    $('#atlas-summary').textContent = `${visible.length} 个技能`;
    $('#catalog-count').textContent = visible.length;
  }

  function renderRelationships() {
    const skill = selectedSkill();
    if (!skill) return;
    const edges = state.relationships.filter((edge) => edge.sourceId === state.selected || edge.targetId === state.selected);
    const typeNames = {delegation:'委派',overlap:'触发重叠',complementarity:'互补','instruction-conflict':'条件性指令冲突',reference:'明确引用','scope-ambiguity':'范围待厘清'};
    const statusNames = {current:'原文可核对',stale:'来源文本或上下文已变更，需复核',unavailable:'证据不可读取',uncertain:'判断待核验'};
    $('#relation-count').textContent = `${edges.length} 条有记录的关系`;
    $('#relationship-map').innerHTML = edges.length ? `<p class="relation-context">${escapeHTML(skill.name)} 的关系依据。源码审阅记录的“原文可核对”表示引文及审阅上下文版本均可核对；明确引用只核对当前引用文本。两者都未验证实际触发结果。</p><div class="relationship-edges">${edges.map((edge) => {
      const evidence = Array.isArray(edge.evidence) ? edge.evidence : [];
      const current = edge.status === 'current';
      const type = typeNames[edge.type] || edge.label || edge.type || '关系待核验';
      const method = edge.method === 'source-review' ? '源码审阅记录' : edge.method === 'explicit-reference' ? '原文明确引用' : '维护分析';
      return `<article class="relationship-edge ${current ? '' : 'evidence-needs-review'}"><div class="edge-route"><button class="relation-node" data-skill-id="${escapeHTML(edge.sourceId)}">${escapeHTML(nameFor(edge.sourceId))}</button><span class="edge-type">${edge.type === 'delegation' || edge.type === 'reference' ? '→' : '↔'} ${escapeHTML(type)}</span><button class="relation-node" data-skill-id="${escapeHTML(edge.targetId)}">${escapeHTML(nameFor(edge.targetId))}</button></div><div class="edge-meta"><span>${escapeHTML(method)}</span><span class="evidence-status">${escapeHTML(statusNames[edge.status] || '判断待核验')}</span></div>${edge.summary ? `<p>${escapeHTML(edge.summary)}</p>` : ''}${edge.condition ? `<p class="edge-condition">适用条件：${escapeHTML(edge.condition)}</p>` : ''}${!current ? '<p class="edge-caution">这条记录需重新核验，不能据此确认当前存在冲突。</p>' : ''}<details class="relationship-evidence"><summary>查看原文证据 · ${evidence.length}</summary>${evidence.length ? evidence.map((entry) => `<div class="evidence-entry"><code>${escapeHTML(entry.path || nameFor(entry.skillId || ''))}${entry.line ? `:${escapeHTML(entry.line)}` : ''}</code><blockquote>${escapeHTML(entry.quote || '未附引文')}</blockquote>${entry.status && entry.status !== 'current' ? `<p class="edge-caution">${escapeHTML(statusNames[entry.status] || '证据待核验')}</p>` : ''}${entry.skillId && skillById(entry.skillId) ? `<button class="secondary-button" data-evidence-skill="${escapeHTML(entry.skillId)}">查看该技能工作流</button>` : ''}</div>`).join('') : '<p>没有附上原文；请先补充依据。</p>'}</details><button class="secondary-button edge-compare" data-compare-pair="${escapeHTML(edge.sourceId)}" data-compare-target="${escapeHTML(edge.targetId)}">加入两项比较</button></article>`;
    }).join('')}</div>` : '<div class="finding-empty">尚无可核对的关系记录。名称相似或属于同一领域，不足以判断委派、重叠或冲突。可选择技能进行比较与 AI 审阅。</div>';
  }

  function addComparison(ids) {
    const candidates = [...new Set([...state.comparison,...ids.filter((id) => skillById(id))])];
    if (candidates.length > 6) { toast('最多比较 6 个技能。请先移除不需要的项目。',true); return; }
    state.comparison = new Set(candidates);
    renderComparison();
    openMaintenance('comparison');
  }

  function renderComparison() {
    const skills = [...state.comparison].map(skillById).filter(Boolean);
    $('#comparison-count').textContent = `${skills.length} / 6`;
    $('#analyze-comparison').disabled = !skills.length || state.busy;
    $('#clear-comparison').disabled = !skills.length;
    $('#compare-selected').disabled = !state.selected || state.comparison.has(state.selected) || skills.length >= 6;
    $('#analyze-button').disabled = !state.selected || state.busy;
    $('#comparison-list').innerHTML = skills.length ? skills.map((skill) => `<article class="comparison-card"><div class="comparison-card-heading"><button data-skill-id="${escapeHTML(idOf(skill))}" class="comparison-name">${escapeHTML(skill.name)}</button><button class="icon-button" data-remove-comparison="${escapeHTML(idOf(skill))}" aria-label="从比较中移除 ${escapeHTML(skill.name)}">×</button></div><p class="comparison-meta">${escapeHTML(sourceNames[skill.source] || skill.source)} · ${escapeHTML(sessionLabel(skill))}</p>${state.drafts.has(idOf(skill)) ? '<span class="dirty-indicator">当前描述草稿</span>' : ''}<p class="comparison-description">${escapeHTML(currentDescription(skill))}</p></article>`).join('') : '<div class="finding-empty">浏览一个技能后点击“加入当前技能”，或在关系记录中加入两项比较。比较本身不会修改源文件。</div>';
  }

  function renderFindings() {
    $('#findings-list').innerHTML = state.findings.length ? state.findings.slice(0,8).map((finding,index) => {
      const ids = finding.skillIds || [];
      const evidence = Array.isArray(finding.evidence) ? finding.evidence.map(textOf).join('\n') : textOf(finding.evidence || '');
      const methodLabel = finding.status === 'stale' || finding.status === 'uncertain' ? '来源已变化 · 待重新读取' : finding.method === 'source-review' ? '历史源码审阅 · 需核对当前证据' : finding.method === 'ai' ? 'AI 建议' : '规则提示';
      return `<article class="finding-card ${finding.type === 'instruction-conflict' ? 'instruction-conflict' : ''}"><span class="finding-symbol" aria-hidden="true">${index % 2 ? '⤨' : '!'}</span><div class="finding-content"><div class="finding-top"><h3 class="finding-title">${escapeHTML(finding.message || finding.title || finding.type)}</h3><span class="finding-type">${escapeHTML(methodLabel)}</span></div><p class="finding-text">${escapeHTML(finding.description || '检查这些技能的适用范围，确认相近描述是否符合预期。')}</p>${evidence ? `<code class="finding-evidence">${escapeHTML(evidence)}</code>` : ''}<div class="finding-skills">${ids.map((id) => `<button data-skill-id="${escapeHTML(id)}">${escapeHTML(nameFor(id))}</button>`).join('')}</div>${finding.suggestion ? `<p class="finding-suggestion">建议：${escapeHTML(textOf(finding.suggestion))}</p>` : ''}</div></article>`;
    }).join('') : '<div class="finding-empty">本轮规则检查没有发现提示项。这不等于已排除语义冲突，可进一步使用 AI 分析具体技能。</div>';
  }

  async function selectSkill(id, {open = true} = {}) {
    const skill = skillById(id);
    if (!skill) return;
    const revision = state.revision;
    state.selected = id;
    if (open) {
      closeMaintenance();
      $('#skill-drawer').hidden = false;
    }
    document.querySelectorAll('.inspector-tabs button').forEach((button) => {
      const active = button.dataset.tab === state.tab;
      button.classList.toggle('active',active);
      button.setAttribute('aria-selected',String(active));
      button.tabIndex = active ? 0 : -1;
    });
    $('#tab-content').setAttribute('aria-labelledby',`tab-${state.tab}`);
    renderList();
    $('#inspector-title').textContent = skill.name;
    $('#skill-monogram').textContent = skill.name.replace(/^.*:/,'').charAt(0).toUpperCase();
    $('#selected-source').textContent = `${sourceNames[skill.source] || skill.source} · ${sessionLabel(skill)} · ${fileLabel(skill)}`;
    $('#editable-badge').textContent = isEditable(skill) ? '可编辑' : '源文件只读';
    renderRelationships();
    renderComparison();
    renderTab();
    if (state.live) {
      try {
        const detail = await api(`/api/skill?id=${encodeURIComponent(id)}`);
        if (revision !== state.revision) return;
        state.details.set(id,detail);
        if (state.drafts.has(id) && state.draftBaselines.get(id) && state.draftBaselines.get(id) !== detail.sha) state.stale.add(id);
        if (typeof detail.description === 'string') skill.description = detail.description;
        if (detail.skill?.body !== undefined) skill.body = detail.skill.body;
        if (typeof detail.intent === 'string') state.intents.set(id,detail.intent);
        if (state.selected === id) { renderTab(); renderComparison(); }
      } catch (error) { toast(error.message, true); }
    }
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll('.inspector-tabs button').forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle('active',active); button.setAttribute('aria-selected',String(active)); button.tabIndex = active ? 0 : -1; });
    $('#tab-content').setAttribute('aria-labelledby',`tab-${tab}`);
    renderTab();
  }

  function intentTemplate(skill) {
    return `# ${skill.name} 的设计意图\n\n状态：待作者确认的编辑器草稿\n\n## 想解决的底层问题\n请说明用户的真实困难，以及为什么需要这个 skill。\n\n## 必须保留的行为\n- \n\n## 应该触发的请求\n- \n\n## 不应该触发的请求\n- \n\n## 容易改坏的边界\n- \n\n## 依据与作者确认\n- 原始 SKILL.md：${skill.path}\n- 尚未将推断视为作者确认。\n`;
  }

  function structuralOutline(body) {
    let fenced = false;
    const headings = [];
    body.split('\n').forEach((line) => {
      if (/^\s*(```|~~~)/.test(line)) { fenced = !fenced; return; }
      if (fenced) return;
      const match = /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line);
      if (match) headings.push({depth:match[1].length,label:match[2]});
    });
    return headings.slice(0,18);
  }

  function renderTab() {
    const skill = selectedSkill();
    if (!skill) return;
    const id = idOf(skill);
    const description = currentDescription(skill);
    const edited = description !== (skill.description || '');
    const node = $('#tab-content');
    if (state.tab === 'overview') {
      const meta = window.SkillAtlas.meta(skill);
      node.innerHTML = `<h3 class="skill-purpose">${escapeHTML(meta.title)}</h3><p class="skill-category-label">${escapeHTML(meta.categoryLabel)}</p><div class="overview-actions"><button id="overview-workflow" class="primary-button">查看正文</button><button id="overview-edit" class="secondary-button">${isEditable(skill) ? '调整触发描述' : '查看触发描述'}</button></div><details class="skill-overview-full"><summary>完整触发描述</summary><p class="skill-description">${escapeHTML(description)}</p></details><div class="overview-secondary-actions"><button id="overview-case">验证使用边界</button><button id="overview-compare">加入比较</button><button id="overview-analyze">AI 检查边界</button></div><span class="detail-label">文件位置</span><code class="source-path">${escapeHTML(skill.path)}</code>`;
      $('#overview-workflow').addEventListener('click',() => setTab('workflow'));
      $('#overview-edit').addEventListener('click',() => setTab('trigger'));
      $('#overview-case').addEventListener('click',() => openMaintenance('cases',{skillIds:[idOf(skill)]}));
      $('#overview-compare').addEventListener('click',() => addComparison([id]));
      $('#overview-analyze').addEventListener('click',() => analyze([id]));
    } else if (state.tab === 'trigger') {
      node.innerHTML = `${!isEditable(skill) ? '<div class="readonly-note">系统与插件源文件在此只读。它们的设计意图仍可单独记录在本项目中。</div>' : ''}<label class="field-label" for="description-editor"><span>什么时候使用它${edited ? '<span class="dirty-indicator">· 有草稿</span>' : ''}</span><span class="field-code">description</span></label><textarea id="description-editor" class="field-input" spellcheck="false" ${isEditable(skill) ? '' : 'readonly'}>${escapeHTML(description)}</textarea><p class="field-help">名称与描述用于 skill 选择。描述越明确，越容易区分相邻能力；最终选择仍由模型结合任务判断。</p><div class="helper-box"><h3><span aria-hidden="true">◇</span> 修改前先看原始意图</h3><p>将“解决什么底层问题”与运行时规则分开。AI 改写只产生建议，应用前可以检查差异，正文保持原样。</p></div><div class="button-row"><button id="preview-button" class="primary-button" ${!isEditable(skill) || state.busy ? 'disabled' : ''}>查看修改预览 <span aria-hidden="true">→</span></button><button id="intent-shortcut" class="secondary-button">查看设计意图</button></div><span class="detail-label">文件位置</span><code class="source-path">${escapeHTML(skill.path)}</code>`;
      $('#description-editor').addEventListener('input',(event) => storeDraft(skill,event.target.value));
      $('#preview-button').addEventListener('click',performPreview);
      $('#intent-shortcut').addEventListener('click',() => setTab('intent'));
    } else if (state.tab === 'workflow') {
      const body = skill.body || state.details.get(id)?.content || '此快照未包含正文。启动本地编辑器后可读取文件。';
      const outline = structuralOutline(body);
      node.innerHTML = `<div class="field-label"><span>正文结构</span><span class="field-code">标题提取 · 非执行顺序</span></div>${outline.length ? `<ol class="workflow-outline">${outline.map((heading) => `<li class="outline-depth-${heading.depth}"><span>${escapeHTML(heading.label)}</span></li>`).join('')}</ol>` : '<p class="field-help">正文没有可提取的 Markdown 标题。</p>'}<div class="field-label"><span>选中后加载的工作流</span><span class="field-code">SKILL.md body</span></div><div class="body-meta"><span>正文只读</span><span>${body.split('\n').length} 行</span></div><pre class="raw-body">${escapeHTML(body)}</pre><p class="field-help">当前版本只修改 description。正文、校准标准、引用与脚本入口不在此处改写。</p>`;
    } else if (state.tab === 'intent') {
      const intent = state.intentDrafts.has(id) ? state.intentDrafts.get(id) : state.intents.get(id) || intentTemplate(skill);
      node.innerHTML = `<p class="intent-info"><strong>编辑器专用 Markdown</strong><br>保存在本项目的 .skill-editor/intents/ 中，运行时 SKILL.md 不引用此文件。空白模板是待确认草稿，请补充原始动机与不可破坏的边界。</p><label class="field-label" for="intent-editor"><span>底层问题与保留约束</span><span class="field-code">EDITOR ONLY</span></label><textarea id="intent-editor" class="intent-input" spellcheck="false">${escapeHTML(intent)}</textarea><div class="button-row"><button id="save-intent" class="primary-button" ${state.busy ? 'disabled' : ''}>${state.live ? '保存设计意图' : '保留在当前页面草稿'}</button></div><p class="field-help">本页草稿在刷新后可能丢失；连接本地编辑器后保存为独立文件。推断的意图需要由作者确认。</p>`;
      $('#intent-editor').addEventListener('input',(event) => state.intentDrafts.set(id,event.target.value));
      $('#save-intent').addEventListener('click',saveIntent);
      if (state.live && !state.intents.has(id)) loadIntent(id);
    } else if (state.tab === 'preview') {
      const preview = state.previews.get(id);
      node.innerHTML = preview ? `<div class="field-label"><span>触发规则修改差异</span><span class="field-code">${state.live ? 'unified diff' : '字段预览'}</span></div><pre class="diff-view">${escapeHTML(preview.diff || 'description 没有变化。')}</pre><div class="preservation-check">${preview.protectedBodyUnchanged ? '工作流正文保持原样；本次改动仅涉及触发描述。' : '预览模式仅比较描述字段，尚未进行服务端校验。'}</div><div class="helper-box"><h3>应用会保留备份</h3><p>${state.live ? '服务端检查源文件版本与可写范围，再保存原文件备份并写入 description。' : '当前为快照预览，未连接文件写入接口。启动本地编辑器后可应用修改。'}</p></div><div class="button-row"><button id="back-to-trigger" class="secondary-button">继续编辑</button><button id="apply-button" class="primary-button" ${!state.live || !isEditable(skill) || !edited || state.busy ? 'disabled' : ''}>应用修改</button></div>` : '<div class="preview-empty">先在“触发规则”中编辑描述，再点击“查看修改预览”。<br>预览会显示改动范围，并核对正文是否保持原样。</div><div class="button-row"><button id="back-to-trigger" class="secondary-button">编辑触发规则 →</button></div>';
      $('#back-to-trigger').addEventListener('click',() => setTab('trigger'));
      $('#apply-button')?.addEventListener('click',applyDescription);
    }
    if (state.stale.has(id) && state.drafts.has(id)) {
      node.insertAdjacentHTML('afterbegin','<div class="stale-draft-note"><p>源文件已在外部变化。描述草稿仍保留，但它基于旧版本，不能直接应用。</p><button id="discard-stale-draft" class="secondary-button">丢弃描述草稿并重读</button></div>');
      $('#discard-stale-draft').addEventListener('click',() => { state.drafts.delete(id); state.draftBaselines.delete(id); state.previews.delete(id); state.stale.delete(id); selectSkill(id); });
    }
    if (state.tab === 'trigger' && !isEditable(skill) && skill.source === 'personal') node.querySelector('.readonly-note').textContent = '该个人技能的文件缺失、不可读或尚未核验，当前不能修改。设计意图仍可独立记录。';
  }

  async function loadIntent(id) {
    try { const result = await api(`/api/intent?id=${encodeURIComponent(id)}`); state.intents.set(id,result.markdown || ''); if (state.selected === id && state.tab === 'intent' && !state.intentDrafts.has(id)) { if (!result.markdown) state.intents.set(id,intentTemplate(skillById(id))); renderTab(); } } catch (error) { state.intents.set(id,intentTemplate(skillById(id))); toast(error.message,true); }
  }

  async function saveIntent() {
    const skill = selectedSkill();
    if (!skill) return;
    const id = idOf(skill);
    const markdown = $('#intent-editor').value;
    state.intentDrafts.set(id,markdown);
    if (!state.live) { toast('草稿已留在当前页面；连接本地编辑器后可保存为文件。'); return; }
    state.busy = true;
    try { await api('/api/intent','PUT',{id,markdown}); state.intents.set(id,markdown); toast('设计意图已单独保存，未写入运行时 skill。'); } catch (error) { toast(error.message,true); } finally { state.busy = false; }
  }

  async function performPreview() {
    const skill = selectedSkill();
    if (!skill || !isEditable(skill) || state.busy) return;
    const id = idOf(skill);
    const description = currentDescription(skill);
    if (!description.trim()) { toast('description 不能为空。', true); return; }
    state.busy = true;
    try {
      let preview;
      if (state.live) {
        const baselineSha = state.draftBaselines.get(id) || state.details.get(id)?.sha || skill.sha256;
        if (!baselineSha) throw new Error('文件版本尚未读取，请重新选择该技能后预览。');
        preview = await api('/api/preview','POST',{id,description,baselineSha});
      }
      else preview = {id,diff:`--- 当前 description\n+++ 草稿 description\n\n- ${skill.description || ''}\n+ ${description}`,protectedBodyUnchanged:false};
      if (currentDescription(skill) !== description) { toast('描述草稿在请求期间变化，请重新预览。'); return; }
      state.previews.set(id,{...preview,description});
      if (state.selected === id) setTab('preview');
    } catch (error) { toast(error.message,true); } finally { state.busy = false; if (state.selected === id) renderTab(); renderComparison(); }
  }

  async function applyDescription() {
    const skill = selectedSkill();
    const id = skill && idOf(skill);
    const preview = state.previews.get(id);
    if (!state.live || !isEditable(skill) || !preview?.baselineSha || state.busy) return;
    const description = currentDescription(skill);
    if (description !== preview.description) { toast('描述与预览不一致，请重新预览后再应用。',true); return; }
    state.busy = true;
    renderTab();
    try {
      const result = await api('/api/apply','POST',{id,description,baselineSha:preview.baselineSha});
      const currentSkill = skillById(id) || skill;
      currentSkill.description = description;
      currentSkill.sha256 = result.sha;
      if (typeof preview.after === 'string') currentSkill.content = preview.after;
      if (currentSkill.frontmatter) currentSkill.frontmatter.description = description;
      state.drafts.delete(id);
      state.draftBaselines.delete(id);
      state.stale.delete(id);
      state.previews.delete(id);
      state.details.set(id,{...state.details.get(id),skill:currentSkill,content:currentSkill.content,sha:result.sha,description});
      markRelationshipsPending(id);
      if (state.selected === id) setTab('trigger');
      renderList();
      renderComparison();
      renderRelationships();
      renderFindings();
      const revision = state.revision;
      try {
        const inventory = await api('/api/inventory');
        if (!Array.isArray(inventory.skills)) throw new Error('目录响应缺少技能列表。');
        if (revision === state.revision) await loadInventory(inventory);
        toast('触发规则已更新，原文件备份已保存；关系与维护提示已重新读取。');
      } catch (error) {
        toast(`触发规则已保存并保留备份。关系与提示尚未重新读取，相关记录已降级待核验：${error.message}`,true);
      }
    } catch (error) { toast(error.message,true); } finally { state.busy = false; if (state.selected === id) renderTab(); renderComparison(); }
  }

  function markRelationshipsPending(id) {
    state.relationships = state.relationships.map((edge) => {
      if (edge.sourceId !== id && edge.targetId !== id && !(edge.evidence || []).some((entry) => entry.skillId === id)) return edge;
      const status = edge.method === 'source-review' ? 'stale' : 'uncertain';
      return {...edge,status,evidence:(edge.evidence || []).map((entry) => entry.skillId === id ? {...entry,status} : entry)};
    });
    state.findings = state.findings.map((finding) => (finding.skillIds || []).includes(id) ? {...finding,status:'stale'} : finding);
    state.inventory.relationships = state.relationships;
    state.inventory.findings = state.findings;
  }

  async function analyze(requestedIds) {
    if (!state.live) { toast('当前为快照预览。请先运行 python3 -m skill_editor，连接本地编辑器。'); return; }
    if (!state.config?.aiConfigured) { toast('请在服务端设置 OPENROUTER_API_KEY 与 OPENROUTER_MODEL，再重启编辑器。密钥无需填入网页。'); return; }
    const ids = Array.isArray(requestedIds) ? requestedIds.filter((id) => skillById(id)) : (state.selected ? [state.selected] : []);
    if (!ids.length || state.busy) return;
    if (ids.some((id) => !isReadable(skillById(id)))) { toast('比较集中有缺失或尚未核验的文件，请先重新扫描并处理提示。',true); return; }
    const descriptions = Object.fromEntries(ids.map((id) => [id,currentDescription(skillById(id))]));
    const baselines = new Map(ids.map((id) => [id,state.draftBaselines.get(id) || state.details.get(id)?.sha || skillById(id).sha256]));
    state.busy = true;
    renderComparison();
    const button = $('#analyze-button');
    button.disabled = true;
    button.textContent = '正在检查边界…';
    try {
      const result = await api('/api/analyze','POST',{ids,descriptions,request:'比较所选 skills 的触发重叠、委派、互补、条件性指令冲突和意图漂移。只为所选技能给出有原文证据的描述建议；区分推断与已确认意图，保留原始约束。用中文回答，不声称完成实际触发回放。'});
      renderAI(result,baselines);
      $('#ai-results').hidden = false;
      openMaintenance('comparison');
      $('#ai-results').scrollIntoView({behavior:'smooth',block:'nearest'});
    } catch (error) { toast(error.message,true); } finally { state.busy = false; button.disabled = false; button.innerHTML = '<span aria-hidden="true">✧</span> AI 检查边界'; renderComparison(); }
  }

  function renderAI(result,baselines = new Map()) {
    const analysis = result.analysis;
    let html = `<span class="small-meta">${escapeHTML(result.provider || 'OpenRouter')} · ${escapeHTML(result.model || '')} · 建议尚未应用</span>`;
    if (!analysis || typeof analysis !== 'object') html += `<pre class="raw-body ai-raw">${escapeHTML(textOf(analysis))}</pre>`;
    else {
      if (analysis.summary) html += `<p class="ai-summary">${escapeHTML(textOf(analysis.summary))}</p>`;
      (analysis.conflicts || []).forEach((conflict) => { html += `<div class="ai-proposal"><h3>${escapeHTML(conflict.type || '待检查的边界')}</h3><p>${escapeHTML(textOf(conflict.evidence || ''))}</p><p>${escapeHTML(textOf(conflict.suggestion || ''))}</p></div>`; });
      (analysis.proposals || []).forEach((proposal,index) => { html += `<div class="ai-proposal"><h3>${escapeHTML(nameFor(proposal.id))}</h3><p>${escapeHTML(textOf(proposal.reason || ''))}</p><pre class="diff-view">${escapeHTML(proposal.description || proposal.proposedDescription || '')}</pre>${proposal.intentPreservation ? `<p>模型对意图保留的判断（建议，尚非证明）：${escapeHTML(textOf(proposal.intentPreservation))}</p>` : ''}${proposal.risks ? `<p>待核对：${escapeHTML(textOf(proposal.risks))}</p>` : ''}<button class="secondary-button" data-proposal="${index}">放入编辑草稿 →</button></div>`; });
      if (!analysis.summary && !(analysis.conflicts || []).length && !(analysis.proposals || []).length) html += `<pre class="raw-body ai-raw">${escapeHTML(textOf(analysis))}</pre>`;
    }
    $('#ai-result-body').innerHTML = html;
    $('#ai-result-body').querySelectorAll('[data-proposal]').forEach((button) => button.addEventListener('click',() => {
      const proposal = analysis.proposals[Number(button.dataset.proposal)];
      const skill = skillById(proposal.id);
      if (!skill || !isEditable(skill)) { toast('此建议没有对应到可编辑的个人技能。', true); return; }
      const proposedDescription = proposal.description || proposal.proposedDescription;
      if (typeof proposedDescription !== 'string' || !proposedDescription.trim()) { toast('AI 建议缺少有效的 description。', true); return; }
      if (!storeDraft(skill,proposedDescription,baselines.get(idOf(skill)))) return;
      state.tab = 'trigger';
      selectSkill(idOf(skill));
      setTab('trigger');
      toast('建议已放入草稿。查看差异后可以应用。');
    }));
  }

  $('#skill-search').addEventListener('input',(event) => { state.query = event.target.value; renderList(); });
  $('#source-filters').addEventListener('click',(event) => {
    const button = event.target.closest('[data-source]');
    if (!button) return;
    state.source = button.dataset.source;
    $('#source-label').textContent = {personal:'我的技能',all:'全部目录',plugin:'插件技能',system:'系统技能'}[state.source];
    document.querySelectorAll('[data-source]').forEach((item) => { const active = item === button; item.classList.toggle('active',active); item.setAttribute('aria-pressed',String(active)); });
    button.closest('details').open = false;
    closeInspector();
    renderList();
  });
  document.addEventListener('click',(event) => {
    const skillButton = event.target.closest('[data-skill-id]');
    if (skillButton) selectSkill(skillButton.dataset.skillId);
    const pair = event.target.closest('[data-compare-pair]');
    if (pair) addComparison([pair.dataset.comparePair,pair.dataset.compareTarget]);
    const remove = event.target.closest('[data-remove-comparison]');
    if (remove) { state.comparison.delete(remove.dataset.removeComparison); renderComparison(); }
    const evidence = event.target.closest('[data-evidence-skill]');
    if (evidence) { selectSkill(evidence.dataset.evidenceSkill); setTab('workflow'); $('#inspector-title').scrollIntoView({behavior:'smooth',block:'nearest'}); }
  });
  document.querySelectorAll('.inspector-tabs button').forEach((button) => button.addEventListener('click',() => setTab(button.dataset.tab)));
  document.querySelector('.inspector-tabs').addEventListener('keydown',(event) => { if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return; event.preventDefault(); const tabs = [...document.querySelectorAll('.inspector-tabs button')]; const current = tabs.indexOf(document.activeElement); const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length; tabs[next].focus(); setTab(tabs[next].dataset.tab); });
  $('#refresh-button').addEventListener('click',refreshInventory);
  $('#compare-selected').addEventListener('click',() => addComparison([state.selected]));
  $('#clear-comparison').addEventListener('click',() => { state.comparison.clear(); renderComparison(); });
  $('#analyze-comparison').addEventListener('click',() => analyze([...state.comparison]));
  $('#analyze-button').addEventListener('click',analyze);
  $('#close-ai').addEventListener('click',() => { $('#ai-results').hidden = true; });
  $('#overview-button').addEventListener('click',() => { closeInspector(); closeMaintenance(); atlas.fit(); });
  $('#open-maintenance').addEventListener('click',() => { if ($('#maintenance-drawer').hidden) openMaintenance(); else closeMaintenance(); });
  $('#close-maintenance').addEventListener('click',closeMaintenance);
  $('#close-inspector').addEventListener('click',() => { closeInspector(); $('#skill-atlas').focus(); });
  document.querySelectorAll('[data-maintenance]').forEach((button) => button.addEventListener('click',() => openMaintenance(button.dataset.maintenance)));
  $('#atlas-zoom-in').addEventListener('click',() => atlas.zoomBy(1.15));
  $('#atlas-zoom-out').addEventListener('click',() => atlas.zoomBy(1 / 1.15));
  $('#atlas-fit').addEventListener('click',() => atlas.fit());
  $('#atlas-reset').addEventListener('click',() => atlas.resetLayout());
  document.addEventListener('keydown',(event) => {
    if (event.key !== 'Escape') return;
    if (!$('#maintenance-drawer').hidden) { closeMaintenance(); $('#open-maintenance').focus(); }
    else if (!$('#skill-drawer').hidden) { closeInspector(); $('#skill-atlas').focus(); }
  });
  document.addEventListener('keydown',(event) => { const tag = document.activeElement?.tagName; if (event.key === '/' && !['INPUT','TEXTAREA'].includes(tag) && !event.metaKey && !event.ctrlKey) { event.preventDefault(); $('#skill-search').focus(); } });
  loadInventory().catch((error) => toast(error.message,true));
})();
