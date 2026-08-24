// Speaker color palette + role helpers (matches V0.12 cloud diarization output).
// Cloud returns: asr_result.speaker_id ("spk_01".."spk_08"), speaker_label ("说话人 1"),
// and result.speaker_roles [{speaker_id, role, description}].

const PALETTE = [
  {key: 'spk_01', ink: '#2f6bf6', soft: '#eaf1ff', dot: '#2f6bf6'},
  {key: 'spk_02', ink: '#18a058', soft: '#e7f6ee', dot: '#18a058'},
  {key: 'spk_03', ink: '#e8730c', soft: '#fdeede', dot: '#e8730c'},
  {key: 'spk_04', ink: '#7c4dff', soft: '#efeaff', dot: '#7c4dff'},
  {key: 'spk_05', ink: '#0ea5a5', soft: '#e3f6f6', dot: '#0ea5a5'},
  {key: 'spk_06', ink: '#e0457b', soft: '#fce8f0', dot: '#e0457b'},
  {key: 'spk_07', ink: '#4f56d6', soft: '#ebecfb', dot: '#4f56d6'},
  {key: 'spk_08', ink: '#a16207', soft: '#f6efdf', dot: '#a16207'},
];

const NEUTRAL = {ink: '#6b7488', soft: '#eef1f6', dot: '#9aa3b7'};

// English roles from DeepSeek -> Chinese display.
const ROLE_LABELS = {
  host: '主持人',
  presenter: '汇报人',
  participant: '参与者',
  customer: '客户',
  'technical expert': '技术专家',
  unknown: '参与者',
};

export function speakerColor(speakerId) {
  if (!speakerId) return NEUTRAL;
  const index = Number.parseInt(String(speakerId).replace(/\D/g, ''), 10);
  if (!Number.isFinite(index) || index < 1) return NEUTRAL;
  return PALETTE[(index - 1) % PALETTE.length];
}

export function roleLabel(role) {
  if (!role) return '';
  const key = String(role).trim().toLowerCase();
  return ROLE_LABELS[key] || role;
}

// Build a map: speaker_id -> {label, role, roleLabel, description, color}.
export function buildSpeakerDirectory({speakers = [], speakerRoles = []} = {}) {
  const roleById = new Map();
  for (const item of speakerRoles) {
    if (item?.speaker_id) roleById.set(item.speaker_id, item);
  }
  const directory = new Map();
  for (const item of speakers) {
    if (!item?.speaker_id) continue;
    const role = roleById.get(item.speaker_id);
    directory.set(item.speaker_id, {
      speakerId: item.speaker_id,
      label: item.label || speakerColorLabel(item.speaker_id),
      segmentCount: item.segment_count || 0,
      role: role?.role || '',
      roleLabel: roleLabel(role?.role),
      description: role?.description || '',
      color: speakerColor(item.speaker_id),
    });
  }
  // Roles may reference speakers not yet in summary list.
  for (const [speakerId, role] of roleById) {
    if (directory.has(speakerId)) continue;
    directory.set(speakerId, {
      speakerId,
      label: speakerColorLabel(speakerId),
      segmentCount: 0,
      role: role.role || '',
      roleLabel: roleLabel(role.role),
      description: role.description || '',
      color: speakerColor(speakerId),
    });
  }
  return directory;
}

function speakerColorLabel(speakerId) {
  const index = Number.parseInt(String(speakerId).replace(/\D/g, ''), 10);
  return Number.isFinite(index) ? `说话人 ${index}` : speakerId;
}

export {speakerColorLabel};
