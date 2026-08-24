import React, {useEffect, useMemo, useState} from 'react';
import {bulkDeleteAgendaItems, createAgendaItem, deleteAgendaItem, listAgendaItems, parseAgendaItem, patchAgendaItem} from './api';

const priorityLabel = {normal: '普通', important: '重要', urgent: '紧急'};
const remindLabel = {none: '不提醒', at_time: '到点提醒', '10m': '提前 10 分钟', '1h': '提前 1 小时', '1d': '提前 1 天'};
const emptyItem = {title: '', due_at: null, assignee: '我', priority: 'normal', remind_mode: 'none', note: '', pinned: false};
const toLocalInput = (iso) => iso ? new Date(new Date(iso).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';

function dueText(value) {
  if (!value) return '未安排时间';
  const d = new Date(value); const now = new Date();
  const hm = d.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
  const same = d.toDateString() === now.toDateString();
  const tomorrow = d.toDateString() === new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1).toDateString();
  if (same) return hm; if (tomorrow) return `明天 ${hm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

function groupKey(item) {
  if (!item.due_at) return 'unscheduled';
  const due = new Date(item.due_at); const now = new Date();
  if (due < now) return 'overdue';
  if (due.toDateString() === now.toDateString()) return 'today';
  return 'later';
}

export default function AgendaPanel() {
  const [items, setItems] = useState([]); const [view, setView] = useState('timeline');
  const [drawer, setDrawer] = useState(null); const [quick, setQuick] = useState('');
  const [selected, setSelected] = useState([]); const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const refresh = async () => { try { setItems((await listAgendaItems()).items || []); setError(''); } catch (e) { setError(e.message); } };
  useEffect(() => { refresh(); const timer = setInterval(refresh, 30000); return () => clearInterval(timer); }, []);
  const open = useMemo(() => items.filter((x) => !x.done), [items]);
  const done = useMemo(() => items.filter((x) => x.done), [items]);
  const groups = useMemo(() => {
    const out = {overdue: [], today: [], later: [], unscheduled: []};
    open.slice().sort((a,b) => Number(b.pinned)-Number(a.pinned) || (a.due_at || '9999').localeCompare(b.due_at || '9999')).forEach((x) => out[groupKey(x)].push(x));
    return out;
  }, [open]);
  const act = async (fn) => { setBusy(true); setError(''); try { await fn(); await refresh(); } catch (e) { setError(e.message); } finally { setBusy(false); } };
  const parseQuick = async (e) => { e.preventDefault(); if (!quick.trim()) return; setBusy(true); try { setDrawer({...emptyItem, ...(await parseAgendaItem(quick.trim()))}); setQuick(''); } catch (err) { setDrawer({...emptyItem, title: quick.trim()}); setError(`自动解析失败，可手动确认：${err.message}`); } finally { setBusy(false); } };
  const save = (form) => act(async () => { const payload = {...form, due_at: form.due_at ? new Date(form.due_at).toISOString() : null, clear_due: !form.due_at}; delete payload.id; delete payload.done; delete payload.created_at; delete payload.updated_at; delete payload.completed_at; delete payload.owner; delete payload.source_event_id; delete payload.remind_at; if (form.id) await patchAgendaItem(form.id, payload); else await createAgendaItem(payload); setDrawer(null); });
  const groupMeta = {overdue:['逾期','需要优先处理'], today:['今天','按时间推进'], later:['之后','按截止时间排列'], unscheduled:['未安排','有空时处理']};
  return <div className="agenda-scroll"><div className="agenda-v15">
    <header className="ag-head"><div><p>{new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'})}</p><h1>议程与待办</h1><span>把有时间和没时间的事情，放进同一条清晰的时间线。</span></div><button className="ag-primary" onClick={() => setDrawer({...emptyItem})}>＋ 新增事项</button></header>
    {error && <div className="ag-error">{error}</div>}
    <section className="ag-summary">
      <button onClick={() => setView('timeline')}><span>逾期</span><b className="red">{groups.overdue.length}</b><small>需要处理</small></button>
      <button onClick={() => setView('timeline')}><span>今天</span><b className="blue">{groups.today.length}</b><small>按时间推进</small></button>
      <button onClick={() => setView('timeline')}><span>之后</span><b>{groups.later.length + groups.unscheduled.length}</b><small>未来安排</small></button>
      <button onClick={() => setView('completed')}><span>已完成</span><b>{done.length}</b><small>查看历史</small></button>
    </section>
    <form className="ag-quick" onSubmit={parseQuick}><i>✦</i><input value={quick} onChange={(e)=>setQuick(e.target.value)} placeholder="例如：明天下午三点提醒我发送报价单"/><span>自动识别时间与内容</span><button disabled={busy}>解析并确认</button></form>
    <section className="ag-card"><nav><div><button className={view==='timeline'?'on':''} onClick={()=>setView('timeline')}>时间线 <em>{open.length}</em></button><button className={view==='completed'?'on':''} onClick={()=>setView('completed')}>已完成 <em>{done.length}</em></button></div><span>默认顺序：置顶 → 逾期 → 截止时间</span></nav>
      {view === 'timeline' ? <div className="ag-timeline">
        {Object.entries(groupMeta).map(([key,meta]) => groups[key].length ? <div className={`ag-group ${key}`} key={key}><aside><b>{meta[0]}</b><small>{meta[1]}</small></aside><div className="ag-items">{groups[key].map(item => <article className={`ag-item ${item.priority}`} key={item.id}>
          <button className="ag-check" title="标记完成" onClick={()=>act(()=>patchAgendaItem(item.id,{done:true}))}/><time>{dueText(item.due_at)}</time><main><div>{item.pinned && <mark>置顶</mark>}<strong>{item.title}</strong>{item.priority!=='normal' && <mark className={item.priority}>{priorityLabel[item.priority]}</mark>}</div><small><i>{(item.assignee||'我').slice(0,1)}</i>负责人：{item.assignee||'我'}　·　{remindLabel[item.remind_mode]||'不提醒'}</small></main><button className="ag-link" onClick={()=>setDrawer({...item,due_at:toLocalInput(item.due_at)})}>编辑</button><button className="ag-pin" onClick={()=>act(()=>patchAgendaItem(item.id,{pinned:!item.pinned}))}>{item.pinned?'取消置顶':'置顶'}</button><button className="ag-delete" onClick={()=>confirm('确定删除这个事项吗？')&&act(()=>deleteAgendaItem(item.id))}>删除</button>
        </article>)}</div></div> : null)}
        {!open.length && <div className="ag-empty">现在没有未完成事项</div>}
      </div> : <div className="ag-history"><header><div><h2>已完成事项</h2><p>完成记录与当前时间线分开管理。</p></div><div><button disabled={!selected.length} onClick={()=>act(()=>bulkDeleteAgendaItems(selected,false).then(()=>setSelected([])))}>删除所选</button><button className="danger" disabled={!done.length} onClick={()=>confirm('确定清空全部已完成事项吗？')&&act(()=>bulkDeleteAgendaItems([],true))}>清空已完成</button></div></header>
        {done.map(item=><article key={item.id}><input type="checkbox" checked={selected.includes(item.id)} onChange={e=>setSelected(e.target.checked?[...selected,item.id]:selected.filter(x=>x!==item.id))}/><b>✓</b><div><strong>{item.title}</strong><small>{item.completed_at?new Date(item.completed_at).toLocaleString():'已完成'} · 负责人：{item.assignee||'我'}</small></div><button onClick={()=>act(()=>patchAgendaItem(item.id,{done:false}))}>恢复</button><button className="danger" onClick={()=>act(()=>deleteAgendaItem(item.id))}>删除</button></article>)}
        {!done.length && <div className="ag-empty">完成的事项会收纳在这里</div>}
      </div>}
    </section>
    {drawer && <ItemDrawer item={drawer} busy={busy} onClose={()=>setDrawer(null)} onSave={save}/>}
  </div></div>;
}

function ItemDrawer({item,busy,onClose,onSave}) {
  const [form,setForm]=useState(item); const set=(key,value)=>setForm({...form,[key]:value});
  return <div className="ag-layer" onMouseDown={e=>e.target===e.currentTarget&&onClose()}><aside className="ag-drawer"><header><div><span>{form.id?'编辑事项':'新增事项'}</span><h2>{form.id?'把信息调整清楚':'记录一件要做的事'}</h2></div><button onClick={onClose}>×</button></header><form onSubmit={e=>{e.preventDefault();onSave(form)}}>
    <label className="wide"><span>事项内容</span><textarea autoFocus required value={form.title} onChange={e=>set('title',e.target.value)}/></label>
    <div className="ag-row"><label><span>截止时间 <em>可不填</em></span><input type="datetime-local" value={form.due_at||''} onChange={e=>set('due_at',e.target.value||null)}/></label><label><span>负责人</span><input value={form.assignee||'我'} onChange={e=>set('assignee',e.target.value)}/></label></div>
    <div className="ag-row"><label><span>优先级</span><select value={form.priority} onChange={e=>set('priority',e.target.value)}><option value="normal">普通</option><option value="important">重要</option><option value="urgent">紧急</option></select></label><label><span>提醒方式</span><select value={form.remind_mode} onChange={e=>set('remind_mode',e.target.value)}><option value="none">不提醒</option><option value="at_time">到点提醒</option><option value="10m">提前 10 分钟</option><option value="1h">提前 1 小时</option><option value="1d">提前 1 天</option></select></label></div>
    <label className="wide"><span>备注 <em>可不填</em></span><textarea value={form.note||''} onChange={e=>set('note',e.target.value)}/></label>
    <div className="ag-model"><b>✦ 统一事项模型</b><span>提醒是事项的属性，不再单独作为分类。</span></div><footer><button type="button" onClick={onClose}>取消</button><button className="ag-primary" disabled={busy}>{form.id?'保存修改':'创建事项'}</button></footer>
  </form></aside></div>;
}
