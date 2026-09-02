import { useState } from 'react';
import { VIS } from '../data';

export default function ContextBusExplorer({ passports }) {
  const [query, setQuery] = useState('');
  const q = query.trim().toLowerCase();
  const visible = q
    ? passports.filter((p) => (p.title + ' ' + p.summary + ' ' + p.team).toLowerCase().includes(q))
    : passports;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <div style={{ background: '#FFFFFF', border: '1px solid #E7E4EE', borderRadius: 16, padding: '20px 24px', display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap' }}>
        <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: '#4A4458', flex: 1, minWidth: 320, textWrap: 'pretty' }}>
          Every card here is an approved answer somebody already worked out. When another developer hits the same problem, the matching card is pulled in automatically — they never have to know it exists.
        </p>
        <input
          className="ob-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search passports…"
          style={{ font: 'inherit', fontSize: 14, padding: '10px 14px', border: '1px solid #DDD8E8', borderRadius: 10, minWidth: 240, outline: 'none', color: '#16121F', background: '#FDFCFE' }}
        />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: 16 }}>
        {visible.map((p) => {
          const v = VIS[p.visibility] || VIS.team;
          return (
            <div key={p.id} className="ob-shadow" style={{ background: '#FFFFFF', border: '1px solid #E7E4EE', borderRadius: 16, padding: 22, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', padding: '3px 9px', borderRadius: 6, background: v.visBg, color: v.visFg }}>{v.visLabel}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11.5, color: '#9A94A8' }}>{p.id}</span>
              </div>
              <div style={{ fontSize: 15.5, fontWeight: 700, lineHeight: 1.35, letterSpacing: '-0.01em', textWrap: 'pretty' }}>{p.title}</div>
              <p style={{ margin: 0, fontSize: 13.5, lineHeight: 1.65, color: '#4A4458', textWrap: 'pretty' }}>{p.summary}</p>
              <div style={{ marginTop: 'auto', paddingTop: 10, borderTop: '1px solid #F0EEF5', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, fontSize: 12, color: '#8A8398' }}>
                <span>{p.team} · approved by {p.approver}</span>
                <span>{p.date}</span>
              </div>
            </div>
          );
        })}
      </div>

      {visible.length === 0 && (
        <div style={{ background: '#FFFFFF', border: '1px dashed #DDD8E8', borderRadius: 16, padding: 48, textAlign: 'center', color: '#8A8398', fontSize: 14.5 }}>
          Nothing matches "{query}". Zero results is a valid answer — the system injects nothing rather than guessing.
        </div>
      )}
    </div>
  );
}
