import React from 'react';

// Minimal inline icon set (stroke-based, currentColor). Keeps JSX readable.
const S = (paths, props = {}) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
       strokeLinecap="round" strokeLinejoin="round" {...props}>{paths}</svg>
);

export const IconCaptions = (p) => S(<><rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M7 14a2.5 2.5 0 1 1 0-4M16 14a2.5 2.5 0 1 1 0-4"/></>, p);
export const IconMinutes = (p) => S(<><rect x="4" y="3" width="16" height="18" rx="2.5"/><path d="M8 8h8M8 12h8M8 16h5"/></>, p);
export const IconMindmap = (p) => S(<><circle cx="6" cy="12" r="2.4"/><circle cx="18" cy="6" r="2.4"/><circle cx="18" cy="18" r="2.4"/><path d="M8.2 11l7.6-3.8M8.2 13l7.6 3.8"/></>, p);
export const IconHistory = (p) => S(<><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 4v4h4"/><path d="M12 8v4l3 2"/></>, p);
export const IconSettings = (p) => S(<><circle cx="12" cy="12" r="3"/><path d="M19.4 13a7.8 7.8 0 0 0 0-2l1.6-1.2-1.8-3.2-1.9.8a7.6 7.6 0 0 0-1.7-1l-.3-2H10l-.3 2a7.6 7.6 0 0 0-1.7 1l-1.9-.8L4.3 9.8 5.9 11a7.8 7.8 0 0 0 0 2l-1.6 1.2 1.8 3.2 1.9-.8a7.6 7.6 0 0 0 1.7 1l.3 2h4l.3-2a7.6 7.6 0 0 0 1.7-1l1.9.8 1.8-3.2z"/></>, p);
export const IconLive = (p) => S(<><path d="M5 9a8 8 0 0 1 14 0"/><path d="M7.5 11a5 5 0 0 1 9 0"/><circle cx="12" cy="14" r="2"/></>, p);
export const IconCloud = (p) => S(<path d="M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1.3A3.5 3.5 0 0 1 17 18z"/>, p);
export const IconCloudCheck = (p) => S(<><path d="M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1.3A3.5 3.5 0 0 1 17 18H7z"/><path d="M9 14l2 2 4-4"/></>, p);
export const IconDownload = (p) => S(<><path d="M12 4v11M7 11l5 5 5-5"/><path d="M5 20h14"/></>, p);
export const IconMic = (p) => S(<><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4"/></>, p);
export const IconSignal = (p) => S(<><path d="M4 17v2M9 13v6M14 9v10M19 5v14"/></>, p);
export const IconAlert = (p) => S(<><path d="M12 3l9 16H3z"/><path d="M12 9v5M12 17h.01"/></>, p);
export const IconChevron = (p) => S(<path d="M6 9l6 6 6-6"/>, p);
export const IconMin = (p) => S(<path d="M5 12h14"/>, p);
export const IconMax = (p) => S(<rect x="5" y="5" width="14" height="14" rx="2"/>, p);
export const IconClose = (p) => S(<path d="M6 6l12 12M18 6L6 18"/>, p);
export const IconPlay = (p) => S(<path d="M7 5l12 7-12 7z" fill="currentColor" stroke="none"/>, p);
export const IconPause = (p) => S(<><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></>, p);
export const IconStop = (p) => S(<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/>, p);
export const IconBookmark = (p) => S(<path d="M6 4h12v16l-6-4-6 4z"/>, p);
export const IconCheck = (p) => S(<path d="M5 13l4 4 10-10"/>, p);
export const IconCheckCircle = (p) => S(<><circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-5"/></>, p);
export const IconCircle = (p) => S(<circle cx="12" cy="12" r="8.5"/>, p);
export const IconDot = (p) => S(<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>, p);
export const IconTarget = (p) => S(<><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4"/></>, p);
export const IconClipboard = (p) => S(<><rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4h6v3H9z"/><path d="M9 12h6M9 16h4"/></>, p);
export const IconRefresh = (p) => S(<><path d="M20 11a8 8 0 0 0-14-4L4 9"/><path d="M4 5v4h4"/><path d="M4 13a8 8 0 0 0 14 4l2-2"/><path d="M20 19v-4h-4"/></>, p);
export const IconSave = (p) => S(<><path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/></>, p);
export const IconCamera = (p) => S(<><rect x="3" y="7" width="18" height="12" rx="2.5"/><circle cx="12" cy="13" r="3"/><path d="M8 7l1.5-2h5L16 7"/></>, p);
export const IconShield = (p) => S(<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>, p);
export const IconServer = (p) => S(<><rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/><path d="M8 7h.01M8 17h.01"/></>, p);
export const IconUser = (p) => S(<><circle cx="12" cy="8" r="4"/><path d="M4 20a8 8 0 0 1 16 0"/></>, p);
export const IconLock = (p) => S(<><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></>, p);
export const IconEye = (p) => S(<><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></>, p);
export const IconEyeOff = (p) => S(<><path d="M3 3l18 18"/><path d="M10.6 6.2A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.3 4M6.3 7.3A17 17 0 0 0 2 12s3.5 7 10 7a10 10 0 0 0 3.9-.8"/><path d="M9.5 9.5a3 3 0 0 0 4.2 4.2"/></>, p);
export const IconLogin = (p) => S(<><path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"/><path d="M3 12h12M11 8l4 4-4 4"/></>, p);
export const IconExport = (p) => S(<><path d="M12 3v12M8 7l4-4 4 4"/><path d="M5 14v5h14v-5"/></>, p);
export const IconTrash = (p) => S(<><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13"/></>, p);
export const IconBack = (p) => S(<path d="M15 5l-7 7 7 7"/>, p);
export const IconExpand = (p) => S(<><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></>, p);
export const IconPlus = (p) => S(<path d="M12 5v14M5 12h14"/>, p);
export const IconLayout = (p) => S(<><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/></>, p);
export const IconCopy = (p) => S(<><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></>, p);
export const IconStar = (p) => S(<path d="M12 4l2.4 5 5.6.6-4 4 1 5.4-5-2.8-5 2.8 1-5.4-4-4 5.6-.6z"/>, p);
export const IconWifiOff = (p) => S(<><path d="M3 3l18 18"/><path d="M5 9a14 14 0 0 1 4-2.3M2 8.8A18 18 0 0 1 6 6M12 20h.01M8.5 16.5a5 5 0 0 1 5-1M19 9a14 14 0 0 0-3-1.8"/></>, p);
export const IconFile = (p) => S(<><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/></>, p);
export const IconControl = (p) => S(<><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/></>, p);
