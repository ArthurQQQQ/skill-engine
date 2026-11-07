/* Editor-only browsing layout. Nodes never execute or modify installed skills. */
(() => {
  'use strict';

  const CATEGORIES = {
    reviews:{label:'投稿评审',caption:'会议与期刊标准',columns:3},
    writing:{label:'写作表达',caption:'结构、文字与图表',columns:3},
    research:{label:'研究判断',caption:'质量、投入与证据',columns:2},
    tools:{label:'开发工具',caption:'应用、文件与算力',columns:3},
    other:{label:'其他',caption:'尚未归类的个人技能',columns:3},
    system:{label:'系统技能',caption:'系统维护 · 源文件只读',columns:3},
    plugins:{label:'插件技能',caption:'插件提供 · 源文件只读',columns:3}
  };
  const ORDER = Object.keys(CATEGORIES);
  const PERSONAL = {
    'aaai-reviewer':['reviews','广义 AI 审稿'],
    'eacl-reviewer':['reviews','EACL 论文审稿'],
    'emnlp-reviewer':['reviews','EMNLP 主会审稿'],
    'emnlp-findings-reviewer':['reviews','Findings 审稿'],
    'emnlp-industry-reviewer':['reviews','产业 NLP 审稿'],
    'icdm-reviewer':['reviews','数据挖掘审稿'],
    'iclr-reviewer':['reviews','机器学习审稿'],
    'tacl-reviewer':['reviews','TACL 期刊审稿'],
    'tmlr-reviewer':['reviews','TMLR 期刊审稿'],
    'wsdm-reviewer':['reviews','搜索推荐审稿'],
    'www-reviewer':['reviews','Web 论文审稿'],
    'citation-management':['writing','引用检索核验'],
    'empirical-ml-paper-writing':['writing','实证论文写作'],
    'humanizer':['writing','文字去机械感'],
    'paper-structure-check':['writing','论文结构重排'],
    'scientific-visualization':['writing','科研图表制作'],
    'scientific-writing':['writing','科学写作审校'],
    'paper-reviewer':['research','通用评审与查新'],
    'reliable-paper-reviewer':['research','论文质量预判'],
    'file-organizer':['tools','文件分类整理'],
    'gpu-combo-selection':['tools','云端 GPU 选配'],
  };
  const PROVIDED = {
    'openai-docs':'产品文档参考','skill-creator':'创建技能','skill-installer':'安装技能',
    'review-agent':'代理审查','documents':'文档编辑','presentations':'演示文稿',
    'pdf':'PDF 处理','spreadsheets':'电子表格','excel-live-control':'Excel 会话控制',
    'visualize':'交互可视化','plugin-management':'插件管理','template-creator':'产物模板',
    'maintain-space':'空间内容维护','manage-schedules':'空间日程管理',
    'organize-space':'空间结构整理','write-page':'页面内容编辑',
    'sites':'网站制作','sites-remix':'网站改编','create-pet':'创建动画宠物',
    'pets':'宠物管理','update-pet':'更新动画宠物'
  };
  const STORAGE_KEY = 'skill-editor.atlas-layout.v1';
  const WIDTH = 478;
  const GAP = 24;
  const MIN_SCALE = .05;
  const MAX_SCALE = 2.4;
  const idOf = (skill) => String(skill.id || skill.path || skill.name || '');
  const baseName = (skill) => String(skill.name || skill.id || '').split(':').pop().toLowerCase();

  function meta(skill = {}) {
    skill = skill || {};
    const name = baseName(skill);
    const mapping = PERSONAL[name];
    const categoryId = skill.source === 'system' ? 'system' : skill.source === 'plugin' ? 'plugins' : mapping?.[0] || 'other';
    return {
      title:mapping?.[1] || PROVIDED[name] || '未分类能力',
      subtitle:String(skill.name || skill.id || '未命名技能'),
      categoryId,
      categoryLabel:CATEGORIES[categoryId].label
    };
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function readLayout() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (saved?.version !== 1 || !saved.positions || typeof saved.positions !== 'object') return {};
      return Object.fromEntries(Object.entries(saved.positions).filter(([key,value]) => ORDER.includes(key) && value && Number.isFinite(value.x) && Number.isFinite(value.y) && Math.abs(value.x) < 100000 && Math.abs(value.y) < 100000));
    } catch (_) { return {}; }
  }

  function create({viewport,world,onSelect,onCategoryChange,onViewportChange} = {}) {
    if (!(viewport instanceof HTMLElement) || !(world instanceof HTMLElement) || world.parentElement !== viewport) throw new TypeError('SkillAtlas requires a viewport and its direct world child.');
    const controller = new AbortController();
    const signal = controller.signal;
    const view = {x:0,y:0,scale:1};
    let data = {skills:[],relationships:[],selectedId:null,query:'',source:'personal'};
    let positions = readLayout();
    const measurements = new Map();
    let groups = new Map();
    let nodes = new Map();
    let gesture = null;
    let suppressClickUntil = 0;
    let renderFrame = 0;
    let fitFrame = 0;
    let destroyed = false;
    let hasFitted = false;
    let visibilityKey = '';
    let structureKey = '';
    let savedTabIndex = viewport.getAttribute('tabindex');
    const svgNS = 'http://www.w3.org/2000/svg';
    const lines = document.createElementNS(svgNS,'svg');
    lines.classList.add('skill-atlas-links');
    lines.setAttribute('aria-hidden','true');
    lines.setAttribute('width','1');
    lines.setAttribute('height','1');
    const layer = element('div','skill-atlas-groups');
    const empty = element('div','skill-atlas-empty');
    empty.hidden = true;
    viewport.classList.add('skill-atlas');
    viewport.tabIndex = 0;
    viewport.setAttribute('role','region');
    viewport.setAttribute('aria-label','技能画布。拖动空白平移，滚轮缩放；加减号缩放，0适配视图。分类仅用于浏览。');
    world.classList.add('skill-atlas-world');
    world.replaceChildren(lines,layer);
    viewport.append(empty);

    function saveLayout() {
      try { localStorage.setItem(STORAGE_KEY,JSON.stringify({version:1,positions})); } catch (_) { /* Layout persistence is optional; skill editing is unaffected. */ }
    }

    function notifyView() {
      if (typeof onViewportChange === 'function') onViewportChange({scale:view.scale,x:view.x,y:view.y});
    }

    function transform() {
      world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
      notifyView();
    }

    function viewportPoint(event) {
      const rect = viewport.getBoundingClientRect();
      return {x:event.clientX - rect.left - viewport.clientLeft,y:event.clientY - rect.top - viewport.clientTop};
    }

    function zoomAt(factor, point) {
      if (!Number.isFinite(factor) || factor <= 0) return;
      const next = Math.max(MIN_SCALE,Math.min(MAX_SCALE,view.scale * factor));
      const ratio = next / view.scale;
      view.x = point.x - (point.x - view.x) * ratio;
      view.y = point.y - (point.y - view.y) * ratio;
      view.scale = next;
      transform();
    }

    function zoomBy(factor) {
      zoomAt(factor,{x:viewport.clientWidth / 2,y:viewport.clientHeight / 2});
    }

    function bounds() {
      const items = [...groups.values()];
      if (!items.length) return null;
      return items.reduce((result,group) => {
        const pos = positions[group.id];
        if (!pos) return result;
        result.left = Math.min(result.left,pos.x);
        result.top = Math.min(result.top,pos.y);
        result.right = Math.max(result.right,pos.x + group.node.offsetWidth);
        result.bottom = Math.max(result.bottom,pos.y + group.node.offsetHeight);
        return result;
      },{left:Infinity,top:Infinity,right:-Infinity,bottom:-Infinity});
    }

    function fit() {
      if (destroyed || !viewport.clientWidth || !viewport.clientHeight) return;
      const box = bounds();
      if (!box || !Number.isFinite(box.left)) { view.x = 0; view.y = 0; view.scale = 1; transform(); return; }
      const horizontal = Math.min(40,viewport.clientWidth * .05);
      const top = Math.min(110,viewport.clientHeight * .23);
      const bottom = Math.min(68,viewport.clientHeight * .14);
      const availableWidth = Math.max(1,viewport.clientWidth - horizontal * 2);
      const availableHeight = Math.max(1,viewport.clientHeight - top - bottom);
      const width = Math.max(1,box.right - box.left);
      const height = Math.max(1,box.bottom - box.top);
      view.scale = Math.max(MIN_SCALE,Math.min(1.15,availableWidth / width,availableHeight / height));
      view.x = horizontal + (availableWidth - width * view.scale) / 2 - box.left * view.scale;
      view.y = top + Math.min(32,Math.max(0,(availableHeight - height * view.scale) / 2)) - box.top * view.scale;
      hasFitted = true;
      transform();
      drawLinks();
    }

    function queueFit() {
      cancelAnimationFrame(fitFrame);
      fitFrame = requestAnimationFrame(() => { fitFrame = 0; if (!destroyed) fit(); });
    }

    function categorySizes() {
      const counts = new Map();
      // Reserve the whole category even when search or source filters hide some
      // of its nodes. A later filter change must not expand it over a new group.
      data.skills.forEach((skill) => {
        const id = meta(skill).categoryId;
        counts.set(id,(counts.get(id) || 0) + 1);
      });
      groups.forEach((group,id) => {
        const columns = CATEGORIES[id].columns;
        const rows = Math.max(1,Math.ceil(group.skills.length / columns));
        const body = group.node.querySelector('.skill-atlas-category-nodes');
        const node = body?.querySelector('.skill-atlas-node');
        const gap = body ? parseFloat(getComputedStyle(body).rowGap) || 7 : 7;
        const pitch = (node?.offsetHeight || 60) + gap;
        measurements.set(id,{
          width:group.node.offsetWidth || WIDTH,
          pitch,
          chrome:Math.max(0,(group.node.offsetHeight || 80 + rows * pitch) - rows * pitch)
        });
      });
      const sizes = new Map();
      ORDER.forEach((id) => {
        const measured = measurements.get(id) || {width:WIDTH,pitch:67,chrome:80};
        const rows = Math.max(1,Math.ceil((counts.get(id) || 0) / CATEGORIES[id].columns));
        sizes.set(id,{width:measured.width,height:measured.chrome + rows * measured.pitch});
      });
      return sizes;
    }

    function placeGroups(force = false) {
      const sizes = categorySizes();
      if (force) groups.forEach((group,id) => { delete positions[id]; });
      function availableAt(x,size,id) {
        let y = 0;
        const occupied = Object.entries(positions).filter(([otherId]) => otherId !== id).map(([otherId,pos]) => ({...pos,...sizes.get(otherId)}));
        // Each move passes at least one rectangle's bottom, so this is bounded
        // by the number of allocated categories, including this render's groups.
        for (let step = 0; step <= occupied.length; step += 1) {
          const conflicts = occupied.filter((rect) => x < rect.x + rect.width + GAP && x + size.width + GAP > rect.x && y < rect.y + rect.height + GAP && y + size.height + GAP > rect.y);
          if (!conflicts.length) return {x,y};
          y = Math.max(...conflicts.map((rect) => rect.y + rect.height + GAP));
        }
        return {x,y};
      }
      groups.forEach((group,id) => {
        if (!positions[id]) {
          const size = sizes.get(id);
          const left = availableAt(0,size,id);
          const right = availableAt(WIDTH + GAP,size,id);
          positions[id] = left.y <= right.y ? left : right;
        }
        // Existing coordinates are preserved, including deliberate overlap or
        // negative positions created by the user while dragging a category.
        group.node.style.left = `${positions[id].x}px`;
        group.node.style.top = `${positions[id].y}px`;
      });
      drawLinks();
    }

    function centreOf(node) {
      const rect = node.getBoundingClientRect();
      const outer = viewport.getBoundingClientRect();
      return {
        x:(rect.left + rect.width / 2 - outer.left - viewport.clientLeft - view.x) / view.scale,
        y:(rect.top + rect.height / 2 - outer.top - viewport.clientTop - view.y) / view.scale,
        width:rect.width / view.scale,
        height:rect.height / view.scale
      };
    }

    function drawLinks() {
      lines.replaceChildren();
      if (!data.selectedId || !nodes.has(data.selectedId)) return;
      const seen = new Set();
      data.relationships.forEach((edge) => {
        if (edge.sourceId !== data.selectedId && edge.targetId !== data.selectedId) return;
        const source = nodes.get(edge.sourceId);
        const target = nodes.get(edge.targetId);
        if (!source || !target) return;
        const key = [edge.sourceId,edge.targetId].sort().join('\n');
        if (seen.has(key)) return;
        seen.add(key);
        const a = centreOf(source);
        const b = centreOf(target);
        const direction = b.x >= a.x ? 1 : -1;
        const start = {x:a.x + direction * a.width / 2,y:a.y};
        const end = {x:b.x - direction * b.width / 2,y:b.y};
        const bend = Math.max(36,Math.abs(end.x - start.x) * .35);
        const path = document.createElementNS(svgNS,'path');
        path.setAttribute('d',`M ${start.x} ${start.y} C ${start.x + direction * bend} ${start.y}, ${end.x - direction * bend} ${end.y}, ${end.x} ${end.y}`);
        path.setAttribute('class',`skill-atlas-link${edge.status !== 'current' ? ' is-uncertain' : ''}`);
        lines.append(path);
      });
    }

    function updateSelection() {
      const related = new Set();
      if (data.selectedId) data.relationships.forEach((edge) => {
        if (edge.sourceId === data.selectedId) related.add(edge.targetId);
        if (edge.targetId === data.selectedId) related.add(edge.sourceId);
      });
      nodes.forEach((node,id) => {
        const selected = id === data.selectedId;
        node.classList.toggle('is-selected',selected);
        node.classList.toggle('is-related',related.has(id));
        node.setAttribute('aria-pressed',String(selected));
      });
      drawLinks();
    }

    function render() {
      if (destroyed) return;
      layer.replaceChildren();
      groups = new Map();
      nodes = new Map();
      const query = String(data.query || '').trim().toLocaleLowerCase();
      const visible = data.skills.filter((skill) => {
        if (data.source && data.source !== 'all' && skill.source !== data.source) return false;
        const info = meta(skill);
        return !query || `${info.title} ${info.subtitle} ${info.categoryLabel} ${skill.description || ''}`.toLocaleLowerCase().includes(query);
      });
      ORDER.forEach((categoryId) => {
        const skills = visible.filter((skill) => meta(skill).categoryId === categoryId);
        if (!skills.length) return;
        const category = CATEGORIES[categoryId];
        const node = element('section','skill-atlas-category');
        node.dataset.atlasCategory = categoryId;
        node.style.width = `${WIDTH}px`;
        node.style.setProperty('--atlas-columns',String(category.columns));
        const grip = element('button','skill-atlas-category-grip');
        grip.type = 'button';
        grip.dataset.atlasGrip = categoryId;
        grip.setAttribute('aria-label',`${category.label}，${skills.length}项。拖动移动分类，聚焦时用方向键移动。`);
        const heading = element('span','skill-atlas-category-label',category.label);
        heading.append(element('span','skill-atlas-category-count',String(skills.length).padStart(2,'0')));
        grip.append(heading,element('span','skill-atlas-category-caption',category.caption));
        const body = element('div','skill-atlas-category-nodes');
        skills.forEach((skill) => {
          const info = meta(skill);
          const id = idOf(skill);
          const button = element('button','skill-atlas-node');
          button.type = 'button';
          button.dataset.atlasSkill = id;
          button.title = `${info.title} · ${info.subtitle}`;
          button.setAttribute('aria-label',`${info.title}，${info.subtitle}`);
          if (skill.fileStatus && skill.fileStatus !== 'present') {
            button.classList.add('has-file-warning');
            button.title += ' · 文件状态需核验';
            button.setAttribute('aria-label',`${info.title}，${info.subtitle}，文件状态需核验`);
          }
          button.append(element('span','skill-atlas-node-title',info.title),element('span','skill-atlas-node-name',info.subtitle));
          body.append(button);
          nodes.set(id,button);
        });
        node.append(grip,body);
        layer.append(node);
        groups.set(categoryId,{id:categoryId,node,skills});
      });
      empty.hidden = visible.length > 0;
      empty.replaceChildren(element('strong','',query ? '没有匹配的技能' : '这个来源还没有技能'),element('span','',query ? '试试名称、中文用途，或缩短搜索词。' : '切换来源或刷新本地盘点。'));
      placeGroups();
      updateSelection();
      const nextVisibilityKey = `${data.source}\n${query}\n${visible.map(idOf).join('\n')}`;
      if (!hasFitted || nextVisibilityKey !== visibilityKey) queueFit();
      visibilityKey = nextVisibilityKey;
    }

    function setData(next = {}) {
      data = {...data,...next};
      data.skills = Array.isArray(data.skills) ? data.skills : [];
      data.relationships = Array.isArray(data.relationships) ? data.relationships : [];
      const nextStructure = JSON.stringify([data.source,data.query,data.skills.map((skill) => [idOf(skill),skill.name,skill.source,skill.fileStatus,skill.description])]);
      if (nextStructure !== structureKey) {
        structureKey = nextStructure;
        cancelAnimationFrame(renderFrame);
        renderFrame = requestAnimationFrame(() => { renderFrame = 0; render(); });
      } else updateSelection();
    }

    function select(id) {
      data.selectedId = id == null ? null : String(id);
      updateSelection();
    }

    function focusSkill(id) {
      const node = nodes.get(String(id));
      if (!node) return;
      const point = centreOf(node);
      view.x = viewport.clientWidth / 2 - point.x * view.scale;
      view.y = (viewport.clientHeight + 42) / 2 - point.y * view.scale;
      transform();
      node.focus({preventScroll:true});
    }

    function resetLayout() {
      positions = {};
      placeGroups(true);
      saveLayout();
      fit();
    }

    function moveCategory(id,x,y) {
      const group = groups.get(id);
      if (!group) return;
      positions[id] = {x,y};
      group.node.style.left = `${x}px`;
      group.node.style.top = `${y}px`;
      drawLinks();
    }

    function finishGesture(event,cancelled = false) {
      if (!gesture || (event.pointerId !== undefined && event.pointerId !== gesture.pointerId)) return;
      const finished = gesture;
      gesture = null;
      viewport.classList.remove('is-panning','is-dragging-category');
      groups.get(finished.categoryId)?.node.classList.remove('is-dragging');
      if (finished.moved) suppressClickUntil = performance.now() + 350;
      if (finished.kind === 'category' && finished.moved) {
        if (cancelled) moveCategory(finished.categoryId,finished.origin.x,finished.origin.y);
        else {
          saveLayout();
          if (typeof onCategoryChange === 'function') onCategoryChange({categoryId:finished.categoryId,...positions[finished.categoryId]});
        }
      }
      try { if (viewport.hasPointerCapture(finished.pointerId)) viewport.releasePointerCapture(finished.pointerId); } catch (_) { /* Pointer may already have been released. */ }
    }

    viewport.addEventListener('pointerdown',(event) => {
      if (event.button !== 0 || !event.isPrimary || event.target.closest('[data-atlas-control]') || gesture) return;
      const grip = event.target.closest('[data-atlas-grip]');
      const node = event.target.closest('[data-atlas-skill]');
      if (!grip && !node && event.target.closest('button,input,textarea,select,a,[contenteditable="true"]')) return;
      const point = viewportPoint(event);
      const categoryId = grip?.dataset.atlasGrip;
      gesture = {
        kind:grip ? 'category' : node ? 'node' : 'pan',
        pointerId:event.pointerId,start:point,moved:false,categoryId,
        origin:grip ? {...positions[categoryId]} : {x:view.x,y:view.y}
      };
      // Keep node pointer capture native so an ordinary button click still fires.
      if (!node) {
        event.preventDefault();
        viewport.setPointerCapture(event.pointerId);
        if (!grip) viewport.focus({preventScroll:true});
      }
    },{signal});

    viewport.addEventListener('pointermove',(event) => {
      if (!gesture || event.pointerId !== gesture.pointerId) return;
      const point = viewportPoint(event);
      const dx = point.x - gesture.start.x;
      const dy = point.y - gesture.start.y;
      if (!gesture.moved && Math.hypot(dx,dy) < 4) return;
      gesture.moved = true;
      if (gesture.kind === 'category') {
        viewport.classList.add('is-dragging-category');
        groups.get(gesture.categoryId)?.node.classList.add('is-dragging');
        moveCategory(gesture.categoryId,gesture.origin.x + dx / view.scale,gesture.origin.y + dy / view.scale);
      } else if (gesture.kind === 'pan') {
        viewport.classList.add('is-panning');
        view.x = gesture.origin.x + dx;
        view.y = gesture.origin.y + dy;
        transform();
      }
    },{signal});
    viewport.addEventListener('pointerup',(event) => finishGesture(event),{signal});
    viewport.addEventListener('pointercancel',(event) => finishGesture(event,true),{signal});
    viewport.addEventListener('lostpointercapture',(event) => finishGesture(event,true),{signal});
    viewport.addEventListener('pointerleave',(event) => { if (gesture?.kind === 'node') finishGesture(event,true); },{signal});
    viewport.addEventListener('click',(event) => {
      const button = event.target.closest('[data-atlas-skill]');
      if (!button || (event.detail !== 0 && performance.now() < suppressClickUntil)) return;
      const id = button.dataset.atlasSkill;
      select(id);
      if (typeof onSelect === 'function') onSelect(id);
    },{signal});
    viewport.addEventListener('wheel',(event) => {
      if (event.target.closest('[data-atlas-control]')) return;
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1);
      zoomAt(Math.exp(Math.max(-1,Math.min(1,-delta * .0015))),viewportPoint(event));
    },{signal,passive:false});
    viewport.addEventListener('keydown',(event) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.target.closest('input,textarea,select,[contenteditable="true"],[data-atlas-control]')) return;
      const grip = event.target.closest('[data-atlas-grip]');
      const arrows = {ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]};
      if (arrows[event.key]) {
        event.preventDefault();
        const [dx,dy] = arrows[event.key];
        const step = event.shiftKey ? 64 : 24;
        if (grip) {
          const id = grip.dataset.atlasGrip;
          const pos = positions[id];
          moveCategory(id,pos.x + dx * step / view.scale,pos.y + dy * step / view.scale);
          saveLayout();
          if (typeof onCategoryChange === 'function') onCategoryChange({categoryId:id,...positions[id]});
        } else { view.x += dx * step; view.y += dy * step; transform(); }
      } else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomBy(1.15); }
      else if (event.key === '-' || event.key === '_') { event.preventDefault(); zoomBy(1 / 1.15); }
      else if (event.key === '0' || event.key === 'Home') { event.preventDefault(); fit(); }
    },{signal});

    const resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
      if (!hasFitted && groups.size) queueFit();
      // After the initial fit, retain the user's pan/zoom when the viewport
      // changes. Fit remains an explicit control, so a drawer or window resize
      // does not unexpectedly move the workspace while the user is inspecting it.
      else drawLinks();
    }) : null;
    resizeObserver?.observe(viewport);
    transform();

    function destroy() {
      destroyed = true;
      controller.abort();
      resizeObserver?.disconnect();
      cancelAnimationFrame(renderFrame);
      cancelAnimationFrame(fitFrame);
      if (gesture) finishGesture({pointerId:gesture.pointerId},true);
      world.replaceChildren();
      world.style.removeProperty('transform');
      empty.remove();
      viewport.classList.remove('skill-atlas','is-panning','is-dragging-category');
      world.classList.remove('skill-atlas-world');
      if (savedTabIndex === null) viewport.removeAttribute('tabindex');
      else viewport.setAttribute('tabindex',savedTabIndex);
    }

    return {setData,select,fit,zoomBy,resetLayout,focusSkill,destroy};
  }

  window.SkillAtlas = {create,meta};
})();
