const label = { fontSize: 11.5, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: '#9A94A8' };

export default function ApprovalInbox({ drafts, onApprove, onReject }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, maxWidth: 1000 }}>
      <div style={{ background: '#FFF9EC', border: '1px solid #F2E2BE', borderRadius: 14, padding: '16px 20px', display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        <span style={{ fontSize: 15, lineHeight: 1.5 }}>⏳</span>
        <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: '#6B5320', textWrap: 'pretty' }}>
          These drafts have passed every automated check and still went nowhere. They stay here until a person approves them — that is the only route into the Context Bus.
        </p>
      </div>

      {drafts.map((d) => (
        <div key={d.id} style={{ background: '#FFFFFF', border: '1px solid #E7E4EE', borderRadius: 16, padding: 24, display: 'flex', flexDirection: 'column', gap: 18 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
              <span style={{ fontSize: 17, fontWeight: 700, letterSpacing: '-0.015em', textWrap: 'pretty' }}>{d.title}</span>
              <span style={{ fontSize: 12.5, color: '#8A8398', fontFamily: "'JetBrains Mono', monospace" }}>{d.id} · session {d.session} · {d.author}</span>
            </div>
            <span style={{ flex: 'none', fontSize: 11.5, fontWeight: 700, padding: '5px 11px', borderRadius: 99, background: '#FDF3E2', color: '#8A5A05', border: '1px solid #F2E2BE' }}>Waiting for approval</span>
          </div>

          <div style={{ background: '#FAF9FD', border: '1px solid #EDEAF4', borderRadius: 12, padding: '16px 18px', fontSize: 14, lineHeight: 1.7, color: '#3A3448', textWrap: 'pretty' }}>{d.summary}</div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 14 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={label}>Sensitive data found</span>
              <span style={{ fontSize: 13.5, color: '#3A3448' }}>{d.flags}</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={label}>Who will see it</span>
              <span style={{ fontSize: 13.5, color: '#3A3448' }}>{d.visibility}</span>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span style={label}>Captured</span>
              <span style={{ fontSize: 13.5, color: '#3A3448' }}>{d.captured}</span>
            </div>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', paddingTop: 18, borderTop: '1px solid #F0EEF5' }}>
            <button className="ob-btn-primary" onClick={() => onApprove(d)} style={{ font: 'inherit', border: 'none', cursor: 'pointer', background: '#6D28D9', color: '#fff', fontSize: 14, fontWeight: 600, padding: '10px 18px', borderRadius: 10 }}>Approve &amp; publish</button>
            <button className="ob-btn-ghost" onClick={() => onReject(d)} style={{ font: 'inherit', cursor: 'pointer', background: '#FFFFFF', border: '1px solid #DDD8E8', color: '#2A2438', fontSize: 14, fontWeight: 600, padding: '10px 18px', borderRadius: 10 }}>Discard</button>
            <span style={{ fontSize: 12.5, color: '#8A8398', fontFamily: "'JetBrains Mono', monospace" }}>same as typing ESDS_APPROVE {d.id}</span>
          </div>
        </div>
      ))}

      {drafts.length === 0 && (
        <div style={{ background: '#FFFFFF', border: '1px solid #E7E4EE', borderRadius: 16, padding: '64px 32px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, textAlign: 'center' }}>
          <div style={{ width: 52, height: 52, borderRadius: 99, background: '#EDF9F3', color: '#0E7C5A', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>✓</div>
          <div style={{ fontSize: 17, fontWeight: 700 }}>Nothing waiting</div>
          <div style={{ fontSize: 14, color: '#6B6580', maxWidth: '44ch', lineHeight: 1.6 }}>Every captured draft has been reviewed. New ones appear here the moment a developer types ESDS_SUBMIT.</div>
        </div>
      )}
    </div>
  );
}
