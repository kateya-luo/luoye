import React, {useMemo, useState} from 'react';
import {composeExport} from './exportComposer';
import {loadExportPreferences, saveExportPreferences} from './exportPreferences';
import {sectionSizes, formatBytes} from './meetingMeta';
import {loadLoginPrefs} from './LoginScreen';
import {IconClose, IconCaptions, IconMinutes, IconMindmap, IconExport, IconFile} from './icons';

const FORMAT_CARDS = [
  {id: 'word', name: 'Word', tag: 'W', color: '#2b579a', enabled: true},
  {id: 'pdf', name: 'PDF', tag: 'PDF', color: '#e8443c', enabled: false, note: '敬请期待'},
  {id: 'markdown', name: 'Markdown', tag: 'M↓', color: '#1d2740', enabled: true},
  {id: 'txt', name: 'TXT', tag: 'T', color: '#6b7488', enabled: true},
];

export default function ExportDialog({meeting, onClose, onExported}) {
  const sizes = useMemo(() => sectionSizes(meeting), [meeting]);
  const initialPreferences = useMemo(() => loadExportPreferences(meeting.session_id), [meeting.session_id]);
  const [format, setFormat] = useState(initialPreferences.format);
  const [sections, setSections] = useState(initialPreferences.sections);
  const [includeInfo, setIncludeInfo] = useState(initialPreferences.includeInfo);
  const [watermark, setWatermark] = useState(initialPreferences.watermark);
  const allChecked = sections.captions && sections.minutes && sections.mindmap;

  const estimate = (sections.captions ? sizes.captions : 0) + (sections.minutes ? sizes.minutes : 0)
    + (sections.mindmap ? sizes.mindmap : 0) + (includeInfo ? 800 : 0);

  const toggleAll = (v) => setSections({captions: v, minutes: v, mindmap: v});
  const doExport = () => {
    saveExportPreferences(meeting.session_id, {format, sections, includeInfo, watermark});
    composeExport(meeting, format, {sections, includeInfo, watermark, account: loadLoginPrefs().account});
    onExported?.();
    onClose();
  };

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="export-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head"><h2>导出会议记录</h2><button className="icon-btn" onClick={onClose}><IconClose /></button></div>
        <div className="panel-body">
          <div className="export-step"><span className="step-n">1</span> 选择导出格式</div>
          <div className="fmt-grid">
            {FORMAT_CARDS.map((f) => (
              <button key={f.id} className={`fmt-card ${format === f.id ? 'active' : ''} ${f.enabled ? '' : 'disabled'}`}
                      disabled={!f.enabled} onClick={() => setFormat(f.id)}>
                <span className="fmt-tag" style={{background: f.color}}>{f.tag}</span>
                <b>{f.name}</b>
                {f.note && <span className="fmt-note">{f.note}</span>}
                {format === f.id && <span className="fmt-check">✓</span>}
              </button>
            ))}
          </div>

          <div className="export-step"><span className="step-n">2</span> 选择导出内容
            <label className="checkbox" style={{marginLeft: 'auto'}}><input type="checkbox" checked={allChecked} onChange={(e) => toggleAll(e.target.checked)} />全选</label>
          </div>
          <div className="export-list">
            <label className="export-item"><input type="checkbox" checked={sections.captions} onChange={(e) => setSections((s) => ({...s, captions: e.target.checked}))} />
              <span className="ei-ic"><IconCaptions /></span><div className="ei-main"><b>字幕记录</b><span>包含会议全过程的实时字幕</span></div><span className="ei-size">{formatBytes(sizes.captions)}</span></label>
            <label className="export-item"><input type="checkbox" checked={sections.minutes} onChange={(e) => setSections((s) => ({...s, minutes: e.target.checked}))} />
              <span className="ei-ic"><IconMinutes /></span><div className="ei-main"><b>会议纪要</b><span>用户选定模板后生成的摘要、结论与待办</span></div><span className="ei-size">{formatBytes(sizes.minutes)}</span></label>
          </div>

          <div className="export-step"><span className="step-n">3</span> 其他选项</div>
          <label className="checkbox" style={{marginBottom: 10}}><input type="checkbox" checked={includeInfo} onChange={(e) => setIncludeInfo(e.target.checked)} />包含会议信息页（会议详情、参会人等）</label>
          <label className="checkbox"><input type="checkbox" checked={watermark} onChange={(e) => setWatermark(e.target.checked)} />包含导出时间水印</label>

          <div className="export-estimate">预估大小：~{formatBytes(estimate)}</div>
        </div>
        <div className="export-foot">
          <button className="btn ghost lg" onClick={onClose}>取消</button>
          <button className="btn primary lg" onClick={doExport}><IconExport /> 导出</button>
        </div>
      </div>
    </div>
  );
}
