const card = {
  background: '#FFFFFF',
  border: '1px solid #E7E4EE',
  borderRadius: 16,
};

const kicker = {
  fontSize: 11.5,
  fontWeight: 700,
  letterSpacing: '0.08em',
  textTransform: 'uppercase',
  color: '#7C3AED',
};

export default function Overview({ go, passportCount, draftCount }) {
  const steps = [
    { n: '1', title: 'Check', trigger: 'automatic, every request', tab: 'xray', body: 'Secrets and personal data are swapped for placeholders on the way out and put back on the way in. The developer sees their real values; the model never does.' },
    { n: '2', title: 'Capture', trigger: 'ESDS_SUBMIT', tab: 'inbox', body: 'When a developer solves something, the answer is drafted as a passport and parked. It reaches the company store only after a person approves it.' },
    { n: '3', title: 'Recall', trigger: 'ESDS_SEARCH', tab: 'bus', body: 'The next person with the same problem gets the approved answer attached to their prompt automatically. They do not have to know it exists.' },
  ];

  const stats = [
    { value: '1,284', label: 'Requests checked', note: 'Last 24 hours, across 9 developers' },
    { value: '37', label: 'Secrets caught', note: '24 of them inside files the agent read' },
    { value: String(passportCount), label: 'Passports available', note: 'Approved answers ready to retrieve' },
    { value: String(draftCount), label: 'Waiting for you', note: 'Nothing is stored until you approve' },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 28 }}>
      <section style={{ ...card, borderRadius: 18, padding: '34px 36px', display: 'grid', gridTemplateColumns: 'minmax(0,1.15fr) minmax(0,1fr)', gap: 40, alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <span style={{ ...kicker, alignSelf: 'flex-start' }}>What this is</span>
          <h2 style={{ margin: 0, fontSize: 30, lineHeight: 1.2, letterSpacing: '-0.03em', fontWeight: 700, textWrap: 'pretty' }}>
            A checkpoint between your developers' AI tools and the model — and the engine that builds your Orgbrain.
          </h2>
          <p style={{ margin: 0, fontSize: 15.5, lineHeight: 1.65, color: '#4A4458', maxWidth: '58ch', textWrap: 'pretty' }}>
            Every request an AI coding tool sends is inspected on the way out. Secrets and personal data are removed before the model sees them. Useful answers are captured, approved, and added to the <strong style={{ color: '#6D28D9' }}>Orgbrain</strong> — your organization's shared brain — so the next person with the same problem never has to solve it twice.
          </p>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 4 }}>
            <button className="ob-btn-primary" onClick={() => go('xray')} style={{ cursor: 'pointer', font: 'inherit', border: 'none', background: '#6D28D9', color: '#fff', fontSize: 14, fontWeight: 600, padding: '11px 20px', borderRadius: 10, boxShadow: '0 4px 12px rgba(109,40,217,0.25)' }}>
              See it on a live request
            </button>
            <button className="ob-btn-ghost" onClick={() => go('inbox')} style={{ cursor: 'pointer', font: 'inherit', background: '#FFFFFF', border: '1px solid #DDD8E8', color: '#2A2438', fontSize: 14, fontWeight: 600, padding: '11px 20px', borderRadius: 10 }}>
              Review pending drafts
            </button>
          </div>
        </div>

        <div style={{ background: '#FAF9FD', border: '1px solid #EDEAF4', borderRadius: 14, padding: 22, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: '#9A94A8' }}>How a developer turns it on</div>
          <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 13, lineHeight: 1.7, background: '#191428', color: '#E9E4F6', padding: '16px 18px', borderRadius: 10, overflowX: 'auto' }}>
            <span style={{ color: '#9A8BC4' }}># one environment variable, nothing else changes</span>
            <br />
            <span style={{ color: '#C4B5FD' }}>ANTHROPIC_BASE_URL</span>=http://localhost:8080 <span style={{ color: '#86EFAC' }}>claude</span>
          </div>
          <div style={{ fontSize: 13.5, lineHeight: 1.6, color: '#4A4458' }}>
            Their editor, commands and habits stay exactly the same. The tool never knows the checkpoint is there.
          </div>
        </div>
      </section>

      <section style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
          <h3 style={{ margin: 0, fontSize: 17, fontWeight: 700, letterSpacing: '-0.01em' }}>The whole system in three steps</h3>
          <span style={{ fontSize: 13.5, color: '#8A8398' }}>Each step is a tab in this dashboard.</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, minmax(0,1fr))', gap: 16 }}>
          {steps.map((st) => (
            <div key={st.n} className="ob-lift" onClick={() => go(st.tab)} style={{ ...card, cursor: 'pointer', padding: 24, display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ width: 26, height: 26, borderRadius: 8, background: '#F1EDFB', color: '#6D28D9', fontSize: 13, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>{st.n}</span>
                <span style={{ fontSize: 16, fontWeight: 700, letterSpacing: '-0.01em' }}>{st.title}</span>
              </div>
              <p style={{ margin: 0, fontSize: 14, lineHeight: 1.6, color: '#4A4458', textWrap: 'pretty' }}>{st.body}</p>
              <div style={{ marginTop: 'auto', paddingTop: 6, fontFamily: "'JetBrains Mono', monospace", fontSize: 12, color: '#6D28D9' }}>{st.trigger}</div>
            </div>
          ))}
        </div>
      </section>

      <section style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0,1fr))', gap: 16 }}>
        {stats.map((k) => (
          <div key={k.label} style={{ ...card, padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.03em' }}>{k.value}</div>
            <div style={{ fontSize: 13.5, fontWeight: 600, color: '#2A2438' }}>{k.label}</div>
            <div style={{ fontSize: 12.5, color: '#8A8398', lineHeight: 1.5 }}>{k.note}</div>
          </div>
        ))}
      </section>

      <section style={{ ...card, borderRadius: 18, padding: '28px 32px', display: 'flex', flexDirection: 'column', gap: 18 }}>
        <h3 style={{ margin: 0, fontSize: 17, fontWeight: 700, letterSpacing: '-0.01em' }}>Two rules worth knowing before you look further</h3>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0,1fr))', gap: 18 }}>
          <div style={{ borderLeft: '3px solid #C4B5FD', padding: '4px 0 4px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ fontSize: 14.5, fontWeight: 700 }}>Nothing is stored without a human</div>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.65, color: '#4A4458' }}>A draft can be perfectly valid and still sit in the inbox. There is exactly one place in the code that writes to the company store, and only a person's approval can reach it.</p>
          </div>
          <div style={{ borderLeft: '3px solid #C4B5FD', padding: '4px 0 4px 16px', display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div style={{ fontSize: 14.5, fontWeight: 700 }}>Redaction is reversible for the developer only</div>
            <p style={{ margin: 0, fontSize: 14, lineHeight: 1.65, color: '#4A4458' }}>Secrets become tokens on the way out and are put back on the way in. The developer sees their real values; the model never does.</p>
          </div>
        </div>
      </section>
    </div>
  );
}
