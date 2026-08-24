import React, {useEffect, useRef, useState} from 'react';

// Live waveform driven by the capture level meter. When idle, shows a flat bar row.
export default function Waveform({level = 0, active = false, bars = 32}) {
  const [heights, setHeights] = useState(() => Array(bars).fill(3));
  const levelRef = useRef(level);
  levelRef.current = level;

  useEffect(() => {
    if (!active) { setHeights(Array(bars).fill(3)); return undefined; }
    const timer = window.setInterval(() => {
      setHeights((prev) => {
        const next = prev.slice(1);
        const base = Math.min(1, levelRef.current * 1.7);
        const jitter = base * (0.45 + Math.random() * 0.55);
        next.push(3 + jitter * 27);
        return next;
      });
    }, 90);
    return () => window.clearInterval(timer);
  }, [active, bars]);

  return (
    <div className={`waveform ${active ? '' : 'idle'}`} aria-hidden="true">
      {heights.map((h, i) => <i key={i} style={{height: `${h}px`}} />)}
    </div>
  );
}
