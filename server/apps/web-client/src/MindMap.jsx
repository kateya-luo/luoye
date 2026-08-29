import React, {useMemo, useState} from 'react';
import {IconLayout, IconPlus, IconExpand, IconDownload} from './icons';

const BRANCH_COLORS = [
  {ink: '#18a058', soft: '#e7f6ee'},
  {ink: '#e8730c', soft: '#fdeede'},
  {ink: '#7c4dff', soft: '#efeaff'},
  {ink: '#0ea5a5', soft: '#e3f6f6'},
  {ink: '#2f6bf6', soft: '#eaf1ff'},
  {ink: '#e0457b', soft: '#fce8f0'},
];

const W = 1180, H = 680, CX = W / 2, CY = H / 2;

// Compute a left/right radial layout from branches.
function layout(branches) {
  const left = [], right = [];
  branches.forEach((b, i) => (i % 2 === 0 ? right : left).push({...b, index: i}));
  const place = (list, side) => {
    const total = Math.max(1, list.reduce((s, b) => s + Math.max(1, (b.items || []).length), 0));
    const top = 60, usable = H - 120, rowH = usable / total;
    let cursor = 0;
    return list.map((b) => {
      const count = Math.max(1, (b.items || []).length);
      const bandTop = top + cursor * rowH;
      const bandH = count * rowH;
      cursor += count;
      const branchX = CX + side * 210;
      const branchY = bandTop + bandH / 2;
      const leafX = CX + side * 430;
      const leaves = (b.items || []).map((item, j) => ({
        text: item,
        x: leafX,
        y: bandTop + rowH * (j + 0.5),
        side,
      }));
      return {...b, side, branchX, branchY, leafX, leaves, color: BRANCH_COLORS[b.index % BRANCH_COLORS.length]};
    });
  };
  return [...place(right, 1), ...place(left, -1)];
}

function curve(x1, y1, x2, y2) {
  const mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

export function MindMapPreview({value}) {
  const branches = (value?.branches || []).slice(0, 4);
  if (!branches.length) return <p className="muted">识别到更多完整语句后生成思维导图</p>;
  return (
    <div className="mm-preview">
      <div className="root">{value?.title || '会议重点'}</div>
      <div className="branches">
        {branches.map((b, i) => {
          const c = BRANCH_COLORS[i % BRANCH_COLORS.length];
          return <div className="branch" key={i} style={{color: c.ink, background: c.soft}}>{b.title}</div>;
        })}
      </div>
    </div>
  );
}

// Full radial mind map view. onSelect(node) reports the focused node to the detail aside.
export default function MindMap({value, onSelect, selectedKey}) {
  const [scale, setScale] = useState(1);
  const title = value?.title || '会议重点';
  const branches = Array.isArray(value?.branches) ? value.branches.slice(0, 6) : [];
  const nodes = useMemo(() => layout(branches), [value]);

  if (!branches.length) {
    return (
      <div className="empty">
        <div className="e-ic"><IconLayout /></div>
        <div>选择模板并生成会议纪要后，这里会展示结构化内容</div>
      </div>
    );
  }

  const rootKey = '__root__';
  return (
    <div className="mm-wrap">
      <svg className="mm-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="mmRoot" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#3b7bff" /><stop offset="1" stopColor="#2f6bf6" />
          </linearGradient>
        </defs>
        <g transform={`translate(${CX} ${CY}) scale(${scale}) translate(${-CX} ${-CY})`}>
          {/* connectors */}
          {nodes.map((b, i) => (
            <g key={`c${i}`}>
              <path d={curve(CX + b.side * 70, CY, b.branchX, b.branchY)} fill="none" stroke={b.color.ink} strokeOpacity="0.5" strokeWidth="2.5" />
              {b.leaves.map((leaf, j) => (
                <path key={j} d={curve(b.branchX + b.side * 64, b.branchY, leaf.x, leaf.y)} fill="none" stroke={b.color.ink} strokeOpacity="0.32" strokeWidth="1.8" />
              ))}
            </g>
          ))}
          {/* root */}
          <foreignObject x={CX - 90} y={CY - 32} width="180" height="64">
            <div onClick={() => onSelect?.({key: rootKey, title, root: true})}
                 style={{height: '100%', display: 'grid', placeItems: 'center', textAlign: 'center',
                         background: 'linear-gradient(135deg,#3b7bff,#2f6bf6)', color: '#fff', borderRadius: 14,
                         fontWeight: 800, fontSize: 16, cursor: 'pointer', boxShadow: '0 8px 22px rgba(47,107,246,.4)'}}>
              {title}
            </div>
          </foreignObject>
          {/* branches */}
          {nodes.map((b, i) => (
            <foreignObject key={`b${i}`} x={b.branchX - 64} y={b.branchY - 19} width="128" height="38">
              <div onClick={() => onSelect?.({key: `b${i}`, title: b.title, items: b.items, color: b.color})}
                   style={{height: '100%', display: 'grid', placeItems: 'center', textAlign: 'center',
                           background: b.color.ink, color: '#fff', borderRadius: 11, fontWeight: 700, fontSize: 13,
                           padding: '0 8px', cursor: 'pointer', overflow: 'hidden'}}>
                {b.title}
              </div>
            </foreignObject>
          ))}
          {/* leaves */}
          {nodes.map((b, i) => b.leaves.map((leaf, j) => (
            <foreignObject key={`l${i}-${j}`} x={leaf.x - (leaf.side > 0 ? 4 : 156)} y={leaf.y - 16} width="160" height="34" className="mm-leaf">
              <div className="mm-leaf-box"
                   onClick={() => onSelect?.({key: `l${i}-${j}`, title: leaf.text, branch: b.title, color: b.color})}
                   style={{height: '100%', display: 'flex', alignItems: 'center',
                           justifyContent: leaf.side > 0 ? 'flex-start' : 'flex-end', textAlign: leaf.side > 0 ? 'left' : 'right',
                           background: '#fff', color: '#46506a', border: `1.5px solid ${b.color.ink}`,
                           borderRadius: 9, fontSize: 12, fontWeight: 600, padding: '0 10px', cursor: 'pointer',
                           boxShadow: selectedKey === `l${i}-${j}` ? `0 0 0 3px ${b.color.soft}` : 'none', overflow: 'hidden'}}>
                <span style={{overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'}}>{leaf.text}</span>
              </div>
            </foreignObject>
          )))}
        </g>
      </svg>
      <div className="mm-zoom no-drag">
        <button onClick={() => setScale((s) => Math.max(0.5, +(s - 0.1).toFixed(2)))}>−</button>
        <span>{Math.round(scale * 100)}%</span>
        <button onClick={() => setScale((s) => Math.min(1.8, +(s + 0.1).toFixed(2)))}>+</button>
      </div>
    </div>
  );
}

export {BRANCH_COLORS};
