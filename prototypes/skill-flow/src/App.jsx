import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Background, BaseEdge, Controls, EdgeLabelRenderer, getSmoothStepPath, Handle, Panel, Position, ReactFlow, ReactFlowProvider, useNodesState, useReactFlow} from '@xyflow/react';
import {CATEGORIES, sources, skillMeta, selectSkills, buildGraph, graphEdges, relatedTo, makePrompt} from './model.mjs';

const LAYOUT_KEY='skill-flow.spike.v1';
const EMPTY=[];
const statusNames={current:'读取目录时引文匹配',stale:'来源已变化',unavailable:'来源不可读取',uncertain:'判断待确认'};

function readPositions() {
  try { const value=JSON.parse(localStorage.getItem(LAYOUT_KEY)); return value && typeof value==='object' && !Array.isArray(value)?value:{}; }
  catch { return {}; }
}

function CategoryNode({data}) {
  return <article className="category-node">
    <div className="category-drag-handle"><span className="node-index">{data.category.mark}</span><span className="drag-grip" aria-hidden="true">⠿</span>
      <h2>{data.category.label}<span>{String(data.count).padStart(2,'0')}</span></h2><p>{data.category.caption}</p></div>
    <div className="category-samples nodrag">{data.samples.map(item=><button key={item.id} onClick={event=>{event.stopPropagation();data.onChoose(item.id);}}>{item.title}<span>↗</span></button>)}</div>
    <button className="category-open nodrag" onClick={event=>{event.stopPropagation();data.onOpen(data.category.id);}}>展开这一类<span aria-hidden="true">→</span></button>
  </article>;
}

function SkillNode({data,selected}) {
  return <article className={`skill-node ${selected?'is-active':''}`}>
    <Handle type="target" position={Position.Left} isConnectable={false}/>
    <div className="skill-node-top"><span className="node-source">{data.sourceLabel}</span><span className="skill-symbol" aria-hidden="true">◇</span></div>
    <h2>{data.title}</h2><p title={data.name}>{data.name}</p><span className="skill-open-label">查看用途与依据 <span aria-hidden="true">↗</span></span>
    <Handle type="source" position={Position.Right} isConnectable={false}/>
  </article>;
}
const nodeTypes={category:CategoryNode,skill:SkillNode};

function EvidenceEdge({id,sourceX,sourceY,targetX,targetY,sourcePosition,targetPosition,style,label,data}) {
  const [path,labelX,labelY]=getSmoothStepPath({sourceX,sourceY,targetX,targetY,sourcePosition,targetPosition});
  return <><BaseEdge id={id} path={path} style={style} interactionWidth={24}/>
    <EdgeLabelRenderer><button className="evidence-edge-label nodrag nopan"
      style={{transform:`translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`}}
      aria-label={`查看${label}的原文依据`}
      onClick={event=>{event.stopPropagation();data.onInspect(data.relation);}}>{label}</button></EdgeLabelRenderer>
  </>;
}
const edgeTypes={evidence:EvidenceEdge};

async function readJson(path,signal) {
  const response=await fetch(path,{signal,cache:'no-store'});
  const result=await response.json();
  if (!response.ok || result.error) throw new Error(result.error || '读取失败，请检查本地服务。');
  return result;
}

function Workspace() {
  const [inventory,setInventory]=useState(null);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [source,setSource]=useState('personal');
  const [category,setCategory]=useState('all');
  const [query,setQuery]=useState('');
  const [selected,setSelected]=useState(null);
  const [detail,setDetail]=useState(null);
  const [detailError,setDetailError]=useState('');
  const [detailLoading,setDetailLoading]=useState(false);
  const [showRelations,setShowRelations]=useState(false);
  const [activeRelation,setActiveRelation]=useState(null);
  const [prompt,setPrompt]=useState('');
  const [notice,setNotice]=useState('');
  const [zoom,setZoom]=useState(1);
  const positions=useRef(null);
  if(!positions.current) positions.current=readPositions();
  const promptRef=useRef(null);
  const searchRef=useRef(null);
  const flowRef=useRef(null);
  const requestRevision=useRef(0);
  const graphApi=useReactFlow();
  const skills=inventory?.skills || EMPTY;
  const relationships=inventory?.relationships || EMPTY;
  const byId=useMemo(()=>new Map(skills.map(item=>[item.id,item])),[skills]);

  const loadInventory=useCallback(async(signal)=>{
    setLoading(true);setError('');
    try {setInventory(await readJson('/api/inventory',signal));}
    catch(err) {if(err.name!=='AbortError') setError(err.message);}
    finally {if(!signal?.aborted) setLoading(false);}
  },[]);
  useEffect(()=>{const controller=new AbortController();loadInventory(controller.signal);return()=>controller.abort();},[loadInventory]);

  const choose=useCallback(id=>{
    const skill=byId.get(id);if(!skill) return;
    if(source!=='all' && source!==skill.source) setSource(skill.source);
    setCategory(skillMeta(skill).categoryId);setQuery('');setSelected(id);setPrompt('');setActiveRelation(null);
  },[byId,source]);
  const openCategory=useCallback(id=>{setCategory(id);setQuery('');setSelected(null);setPrompt('');setActiveRelation(null);},[]);
  const graph=useMemo(()=>buildGraph(skills,{source,category,query,positions:positions.current}),[skills,source,category,query]);
  const [nodes,setNodes,onNodesChange]=useNodesState([]);
  useEffect(()=>{
    setNodes(graph.nodes.map(node=>({...node,data:{...node.data,onChoose:choose,onOpen:openCategory}})));
  },[graph,choose,openCategory,setNodes]);
  useEffect(()=>{
    setNodes(items=>items.map(node=>({...node,selected:node.id===selected})));
  },[selected,graph,setNodes]);
  // No skill-endpoint edges can exist in the category-only overview.
  const edges=useMemo(()=>graph.overview?[]:graphEdges(relationships,graph.visibleSkills,selected,showRelations).map(edge=>({...edge,type:'evidence',data:{...edge.data,onInspect:setActiveRelation}})),[graph,relationships,selected,showRelations]);
  const related=useMemo(()=>relatedTo(relationships,selected),[relationships,selected]);
  const fit=useCallback(()=>graphApi.fitView({padding:0.22,minZoom:0.2,maxZoom:1,duration:250}),[graphApi]);
  useEffect(()=>{
    if(!graph.nodes.length) return;
    let second;
    const first=requestAnimationFrame(()=>{second=requestAnimationFrame(()=>fit());});
    return()=>{cancelAnimationFrame(first);cancelAnimationFrame(second);};
  },[graph,fit]);

  useEffect(()=>{
    const revision=++requestRevision.current;
    setDetail(null);setDetailError('');setPrompt('');
    if(!selected){setDetailLoading(false);return;}
    const controller=new AbortController();setDetailLoading(true);
    readJson(`/api/skill?id=${encodeURIComponent(selected)}`,controller.signal)
      .then(value=>{if(revision===requestRevision.current) setDetail(value);})
      .catch(err=>{if(err.name!=='AbortError' && revision===requestRevision.current) setDetailError(err.message);})
      .finally(()=>{if(revision===requestRevision.current) setDetailLoading(false);});
    return()=>controller.abort();
  },[selected,inventory]);

  const closeDetail=useCallback(()=>{
    setSelected(null);setActiveRelation(null);setPrompt('');
    requestAnimationFrame(()=>flowRef.current?.focus());
  },[]);
  useEffect(()=>{
    const handler=event=>{
      if(event.key==='Escape') closeDetail();
      if(event.key==='/' && !event.metaKey && !event.ctrlKey && !event.altKey && !event.target.closest('input,textarea,select,[contenteditable="true"]')) {
        event.preventDefault();searchRef.current?.focus();
      }
    };
    document.addEventListener('keydown',handler);return()=>document.removeEventListener('keydown',handler);
  },[closeDetail]);
  useEffect(()=>{if(!notice)return;const timer=setTimeout(()=>setNotice(''),4500);return()=>clearTimeout(timer);},[notice]);

  function savePosition(_,node) {
    positions.current={...positions.current,[node.id]:{x:node.position.x,y:node.position.y}};
    try{localStorage.setItem(LAYOUT_KEY,JSON.stringify(positions.current));}catch{setNotice('浏览器未允许保存布局，本次拖动仍然有效。');}
  }
  function resetLayout() {
    positions.current={};
    try{localStorage.removeItem(LAYOUT_KEY);}catch{}
    setNodes(buildGraph(skills,{source,category,query}).nodes.map(node=>({...node,selected:node.id===selected,data:{...node.data,onChoose:choose,onOpen:openCategory}})));
    requestAnimationFrame(()=>fit());
  }
  async function copyPrompt() {
    try {await navigator.clipboard.writeText(prompt);setNotice('提示已复制，请粘贴到 Codex 对话。网页没有发送消息。');}
    catch {promptRef.current?.focus();promptRef.current?.select();setNotice('提示已选中，可手动复制到 Codex。');}
  }
  const currentDetail=detail?.skill?.id===selected?detail:null;
  const selectedSkill=currentDetail?.skill || byId.get(selected);
  const selectedMeta=selectedSkill?skillMeta(selectedSkill):null;
  const categoryTitle=CATEGORIES.find(item=>item.id===category)?.label;
  const sourceSkills=selectSkills(skills,{source});
  const visibleCount=graph.visibleSkills.length;

  return <div className="engine-app">
    <aside className="engine-nav">
      <a className="engine-brand" href="/"><span className="brand-glyph">s<span>e</span></span><span>Skill Engine<small>PERSONAL CAPABILITIES</small></span></a>
      <div className="nav-section-label">能力地图</div>
      <button className={`nav-item ${category==='all'?'active':''}`} onClick={()=>openCategory('all')}><span>◈</span>全部用途<small>{sourceSkills.length}</small></button>
      <nav aria-label="用途分类">{CATEGORIES.map(group=>{
        const count=sourceSkills.filter(skill=>skillMeta(skill).categoryId===group.id).length;
        return count>0 && <button key={group.id} className={`nav-item ${category===group.id?'active':''}`} onClick={()=>openCategory(group.id)}><span className="nav-number">{group.mark}</span>{group.label}<small>{count}</small></button>;
      })}</nav>
      <div className="nav-bottom"><div className="nav-purpose"><span>看懂 · 分清 · 稳妥维护</span><p>从一堆技能文件，到你理解的个人能力库。</p></div><a href="/">↖ 返回现有编辑器</a><p className="prototype-label">REACT FLOW · 体验原型</p></div>
    </aside>

    <main className="engine-main">
      <header className="engine-header"><div className="header-location"><span>工作台</span><span>/</span><strong>{categoryTitle || '我的能力地图'}</strong></div>
        <div className="header-actions"><select aria-label="技能来源" value={source} onChange={event=>{setSource(event.target.value);openCategory('all');}}>{sources.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select><button className="icon-button" aria-label="重读已盘点目录" title="重读已有目录成员，不扫描新增文件" disabled={loading} onClick={()=>loadInventory()}>↻</button><span className="readonly-badge">只读浏览</span></div>
      </header>
      <section className="flow-stage" aria-label="能力地图">
        <div className="stage-intro"><p className="eyebrow">YOUR SKILL LANDSCAPE</p><h1>{query?'找到相关能力':categoryTitle || '让能力，有迹可循。'}</h1><p>{graph.overview?'从用途开始，发现你已经拥有的技能。':'点击卡片，查看用途、来源与原文依据。'}<span className="count-label">{visibleCount} 项已盘点技能</span></p></div>
        <div className="stage-search"><label><span aria-hidden="true">⌕</span><input ref={searchRef} aria-label="搜索技能" placeholder="搜索名称、用途或关键词" value={query} onChange={event=>{setQuery(event.target.value);setSelected(null);setActiveRelation(null);}}/><kbd>/</kbd></label>{query && <button aria-label="清空搜索" onClick={()=>setQuery('')}>×</button>}</div>
        <div className="flow-canvas" ref={flowRef} tabIndex={-1} onKeyDownCapture={event=>{
          if(event.key!=='Enter' && event.key!==' ') return;
          const element=event.target.closest('.react-flow__node');
          if(!element || event.target.closest('button,input,textarea,select')) return;
          const node=nodes.find(item=>item.id===element.dataset.id);
          if(node){event.preventDefault();event.stopPropagation();node.type==='category'?openCategory(node.data.category.id):choose(node.id);}
        }}>
          <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} onNodesChange={onNodesChange}
            onNodeClick={(_,node)=>node.type==='category'?openCategory(node.data.category.id):choose(node.id)}
            onNodeDoubleClick={(_,node)=>node.type==='category'?openCategory(node.data.category.id):choose(node.id)}
            onNodeDragStop={savePosition}
            onEdgeClick={(_,edge)=>setActiveRelation(edge.data.relation)} onPaneClick={()=>setActiveRelation(null)}
            onMove={(_,viewport)=>setZoom(viewport.zoom)} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
            minZoom={0.15} maxZoom={1.7} colorMode="dark" zoomOnDoubleClick={false} fitView
            fitViewOptions={{padding:0.22,maxZoom:1}} ariaLabelConfig={{'controls.zoomIn.ariaLabel':'放大画布','controls.zoomOut.ariaLabel':'缩小画布','controls.fitView.ariaLabel':'适配当前视图'}}>
            <Background color="#26323d" gap={24} size={1}/>
            <Controls showInteractive={false}/>
            <Panel position="bottom-center" className="canvas-state"><span>{Math.round(zoom*100)}%</span><span>拖动平移 · 滚轮缩放</span><button onClick={resetLayout}>恢复布局</button></Panel>
          </ReactFlow>
        </div>
        {(loading || error || (!loading && !visibleCount)) && <div className="stage-message" role="status"><span className="message-symbol">◇</span><h2>{loading?'正在读取你的能力库':error?'暂时无法读取':'没有匹配的技能'}</h2><p>{error || (loading?'读取已有盘点中的当前文件。':'尝试更短的关键词，或换一个用途分类。')}</p>{error && <button onClick={()=>loadInventory()}>重新读取</button>}</div>}
        <footer className="stage-footnote"><span>分类为浏览标注，关系线不表示执行顺序。</span><span>目录与缓存存在不等于当前会话可用。</span></footer>
      </section>
    </main>

    {selected && <aside className="detail-panel" aria-label="技能详情">
      <div className="detail-top"><span>CAPABILITY / DETAILS</span><button className="icon-button" aria-label="关闭技能详情" onClick={closeDetail}>×</button></div>
      <div className="relation-toggle detail-map-tools"><label><input type="checkbox" checked={showRelations} onChange={event=>setShowRelations(event.target.checked)}/>所选关系</label><span>同一视图 · {edges.length} 条</span></div>
      <div className="detail-scroll">
        <span className="detail-source">{selectedMeta?.sourceLabel || '读取中'} · 原型只读</span><h2>{selectedMeta?.title || '技能详情'}</h2><p className="detail-name">{selectedMeta?.name}</p>
        {detailLoading && <p role="status" className="muted">正在核对当前原文…</p>}{detailError && <p role="alert" className="warning">{detailError}</p>}
        {currentDetail && <><div className="detail-section"><h3>它适合做什么</h3><p className="description-text">{currentDetail.description}</p><small className="muted">以上为当前技能 description；用途标题是编辑器标注。</small></div>
          <div className="source-facts"><span>{selectedMeta.sessionLabel}</span><span>{selectedMeta.statusLabel}</span><p>不代表本次已经加载或执行。</p><details><summary>来源与版本</summary><code>{currentDetail.skill.path}</code><code>SHA {currentDetail.sha}</code></details></div>
          <button className="codex-button" onClick={()=>setPrompt(makePrompt({skill:currentDetail.skill}))}>在 Codex 里讨论这个技能 <span>↗</span></button><p className="handoff-hint">生成提示，复制到当前对话后继续。</p>
          {prompt && <div className="prompt-box"><textarea ref={promptRef} aria-label="交给 Codex 的提示" value={prompt} readOnly rows={7}/><button onClick={copyPrompt}>复制提示</button><small>不会自动发送或调用模型。</small></div>}
          <details className="detail-section raw-content"><summary>查看当前 SKILL.md 原文</summary><pre>{currentDetail.content}</pre></details>
        </>}
        <div className="detail-section"><div className="section-title"><h3>相邻能力与边界</h3><span>{related.length}</span></div><p className="muted">关系来自原文审阅或明确引用，需结合任务判断。</p>
          {related.length===0?<p className="empty-related">当前没有已记录的关系，不据此推断它与其他技能独立。</p>:related.map(relation=>{
            const neighbor=relation.sourceId===selected?relation.targetId:relation.sourceId;
            return <article className={`relation-card ${activeRelation?.id===relation.id?'focused':''}`} key={relation.id}><div><strong>{relation.label || relation.type}</strong><span className={relation.status==='current'?'':'warning'}>{statusNames[relation.status] || '待核对'}</span></div><button onClick={()=>choose(neighbor)} disabled={!byId.has(neighbor)}>{skillMeta(byId.get(neighbor) || {id:neighbor}).title} ↗</button><p>{relation.summary}</p>{relation.condition && <p className="muted">适用条件：{relation.condition}</p>}<details open={activeRelation?.id===relation.id || undefined}><summary>查看原文依据</summary>{(relation.evidence || []).map((item,index)=><blockquote key={index}><code>{item.path}{item.line?`:${item.line}`:''}</code><p>{item.quote}</p><small>{statusNames[item.status] || '待核对'}</small></blockquote>)}<small>引文匹配不等于实际触发或行为验证。</small></details></article>;
          })}
        </div>
      </div><div className="detail-footer">浏览与理解，由你决定下一步。</div>
    </aside>}
    {notice && <div role="status" className="engine-toast">{notice}</div>}
  </div>;
}

export default function App(){return <ReactFlowProvider><Workspace/></ReactFlowProvider>;}
