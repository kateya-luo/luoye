const STORAGE_KEY = 'clear-meeting-export-preferences-v1';
const FORMATS = new Set(['word', 'markdown', 'txt', 'json']);

export const DEFAULT_EXPORT_PREFERENCES = Object.freeze({
  format: 'word',
  sections: Object.freeze({captions: true, minutes: true, mindmap: true}),
  includeInfo: true,
  watermark: true,
});

function normalize(value = {}) {
  const sections = value.sections || {};
  return {
    format: FORMATS.has(value.format) ? value.format : DEFAULT_EXPORT_PREFERENCES.format,
    sections: {
      captions: typeof sections.captions === 'boolean' ? sections.captions : true,
      minutes: typeof sections.minutes === 'boolean' ? sections.minutes : true,
      mindmap: typeof sections.mindmap === 'boolean' ? sections.mindmap : true,
    },
    includeInfo: typeof value.includeInfo === 'boolean' ? value.includeInfo : true,
    watermark: typeof value.watermark === 'boolean' ? value.watermark : true,
  };
}

export function loadExportPreferences(sessionId) {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    return normalize(stored[String(sessionId || '')]);
  } catch {
    return normalize();
  }
}

export function saveExportPreferences(sessionId, value) {
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    stored[String(sessionId || '')] = normalize(value);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  } catch { /* localStorage may be unavailable in restricted webviews */ }
}
