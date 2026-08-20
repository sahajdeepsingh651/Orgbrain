import { useState, useEffect } from 'react';
import { REQUESTS } from '../data';

const mono = { fontFamily: "'JetBrains Mono', monospace" };

function Segments({ segs, hotStyle }) {
  return (
    <div style={{ background: '#FDFCFE', border: '1px solid #EDEAF4', borderRadius: 12, padding: '16px 18px', fontSize: 14, lineHeight: 1.8, whiteSpace: 'pre-wrap', wordBreak: 'break-word', ...mono }}>
      {segs.map((s, i) =>
        s.hot ? (
          <span key={i} style={hotStyle}>{s.v}</span>
        ) : (
          <span key={i} style={{ color: '#3A3448' }}>{s.v}</span>
        )
      )}
    </div>
  );
}

const th = { padding: '10px 14px', background: '#FAF9FD', fontSize: 11.5, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: '#8A8398' };
const td = { padding: '12px 14px', borderTop: '1px solid #EDEAF4', fontSize: 13 };

const formatTextToSegments = (text) => {
  if (!text) return [];
  const parts = text.split(/(⟦(?:SECRET|PII)_[0-9]+⟧)/g);
  return parts.map((part) => ({
    v: part,
    hot: part.startsWith('⟦SECRET_') || part.startsWith('⟦PII_')
  }));
};

const extractImportantParts = (messages) => {
  if (!Array.isArray(messages) || messages.length === 0) return { prompt: '', injection: '' };
  let prompt = '';
  let injection = '';
  const lastUserMsg = [...messages].reverse().find(m => m.role === 'user');
  if (lastUserMsg) {
    if (Array.isArray(lastUserMsg.content)) {
      const textBlocks = lastUserMsg.content.filter(b => b.type === 'text');
      for (let i = textBlocks.length - 1; i >= 0; i--) {
        let text = textBlocks[i].text || '';
        if (text.includes('**Automated Instruction:**') || text.includes('<system-reminder>')) {
          injection = text;
        } else if (!text.includes('SUGGESTION MODE:') && !prompt) {
          prompt = text;
        }
      }
    } else if (typeof lastUserMsg.content === 'string') {
      prompt = lastUserMsg.content;
    }
  }
  const systemMsgs = messages.filter(m => m.role === 'system');
  for (const systemMsg of systemMsgs) {
    let text = '';
    if (Array.isArray(systemMsg.content)) {
      text = systemMsg.content.map(b => b.text).join('\n');
    } else if (typeof systemMsg.content === 'string') {
      text = systemMsg.content;
    }
    
    if (text.includes('ESDS') || text.includes('Data Passport') || text.includes('DataPassport') || text.includes('Automated Instruction')) {
      injection = text;
      break;
    }
  }
  return { prompt, injection };
};

export default function XRayMonitor() {
  const [requests, setRequests] = useState(REQUESTS);
  const [selId, setSelId] = useState('r2');

  useEffect(() => {
    const eventSource = new EventSource(`http://${window.location.hostname}:8080/v1/dashboard/stream`);
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'request') {
          const rawParts = extractImportantParts(data.raw?.messages);
          const safeParts = extractImportantParts(data.sanitized?.messages);
          if (rawParts.prompt) {
            const newReq = {
              id: 'req_' + Date.now(),
              label: 'Live Request',
              time: new Date().toLocaleTimeString(),
              blurb: 'Intercepted from gateway.',
              tag: safeParts.injection ? 'Context injected' : (safeParts.prompt !== rawParts.prompt ? 'Redacted' : 'Clean'),
              who: 'live · claude code',
              raw: [{ v: rawParts.prompt, hot: false }],
              safe: formatTextToSegments(safeParts.prompt),
              hasInjection: !!safeParts.injection,
              injection: safeParts.injection,
              explain: 'Live traffic captured by the gateway.',
              findings: [],
              note: 'Monitoring real-time API traffic.'
            };
            setRequests(prev => [newReq, ...prev]);
            setSelId(newReq.id);
          }
        }
      } catch (e) {}
    };
    return () => eventSource.close();
  }, []);

  const sel = requests.find((r) => r.id === selId) || requests[0] || REQUESTS[0];

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24, alignItems: 'flex-start' }}>
      <div style={{ flex: '1 1 260px', maxWidth: 340, minWidth: 240, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ fontSize: 11.5, fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: '#9A94A8', paddingLeft: 4 }}>Intercepted requests</div>
        {requests.map((rq) => {
          const on = rq.id === selId;
          return (
            <div key={rq.id} onClick={() => setSelId(rq.id)} style={{ cursor: 'pointer', background: on ? '#FFFFFF' : '#FBFAFD', border: `1px solid ${on ? '#6D28D9' : '#E7E4EE'}`, borderRadius: 13, padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 7 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontSize: 14, fontWeight: 650, letterSpacing: '-0.01em' }}>{rq.label}</span>
                <span style={{ ...mono, fontSize: 11, color: '#9A94A8' }}>{rq.time}</span>
              </div>
              <div style={{ fontSize: 12.5, color: '#6B6580', lineHeight: 1.5 }}>{rq.blurb}</div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 11, fontWeight: 600, padding: '3px 8px', borderRadius: 6, background: rq.hasInjection ? '#F1EDFB' : '#FCEDF2', color: rq.hasInjection ? '#5B21B6' : '#B0184F' }}>{rq.tag}</span>
                <span style={{ ...mono, fontSize: 11, fontWeight: 500, padding: '3px 8px', borderRadius: 6, background: '#F2F0F6', color: '#6B6580' }}>{rq.who}</span>
              </div>
            </div>
          );
        })}
        <div style={{ border: '1px dashed #DDD8E8', borderRadius: 13, padding: '14px 16px', fontSize: 12.5, lineHeight: 1.55, color: '#8A8398' }}>
          With the gateway running, new requests stream in here the moment a developer presses enter.
        </div>
      </div>

      <div style={{ flex: '999 1 560px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 18 }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 16, alignItems: 'stretch' }}>
          <div style={{ background: '#FFFFFF', border: '1px solid #F0D3DE', borderTop: '3px solid #D6336C', borderRadius: 16, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 15, fontWeight: 700 }}>On the developer's machine</span>
                <span style={{ fontSize: 12.5, color: '#8A8398' }}>What the tool actually tried to send</span>
              </div>
              <span style={{ fontSize: 11.5, fontWeight: 700, padding: '4px 10px', borderRadius: 99, background: '#FCEDF2', color: '#B0184F', border: '1px solid #F3CFDD' }}>Sensitive</span>
            </div>
            <Segments segs={sel.raw} hotStyle={{ background: '#FCE7EF', color: '#A3154A', border: '1px solid #F3C9D9', borderRadius: 5, padding: '1px 5px', fontWeight: 600 }} />
          </div>

          <div style={{ background: '#FFFFFF', border: '1px solid #DED2F7', borderTop: '3px solid #6D28D9', borderRadius: 16, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <span style={{ fontSize: 15, fontWeight: 700 }}>What the model receives</span>
                <span style={{ fontSize: 12.5, color: '#8A8398' }}>Sent to api.anthropic.com</span>
              </div>
              <span style={{ fontSize: 11.5, fontWeight: 700, padding: '4px 10px', borderRadius: 99, background: '#EDF9F3', color: '#0E7C5A', border: '1px solid #CDEBDD' }}>Cleared</span>
            </div>
            <Segments segs={sel.safe} hotStyle={{ background: '#F1EAFE', color: '#5B21B6', border: '1px solid #DCCCFA', borderRadius: 5, padding: '1px 5px', fontWeight: 700 }} />
            {sel.hasInjection && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: '#6D28D9' }}>Context added by Data Passport</div>
                <div style={{ ...mono, background: '#F7F3FF', border: '1px solid #E2D6FB', borderLeft: '3px solid #7C3AED', borderRadius: 10, padding: '14px 16px', fontSize: 13, lineHeight: 1.7, color: '#3A2E55', whiteSpace: 'pre-wrap' }}>{sel.injection}</div>
                <div style={{ fontSize: 12.5, color: '#8A8398', lineHeight: 1.55 }}>Appended after the cached part of the conversation, so answering with company context costs nothing extra.</div>
              </div>
            )}
          </div>
        </div>

        <div style={{ background: '#FFFFFF', border: '1px solid #E7E4EE', borderRadius: 16, padding: '22px 24px', display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <span style={{ fontSize: 15, fontWeight: 700 }}>What happened on this request</span>
            <span style={{ fontSize: 13, color: '#6B6580' }}>{sel.explain}</span>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.4fr) minmax(0,1.1fr) minmax(0,0.9fr) minmax(0,0.8fr)', border: '1px solid #EDEAF4', borderRadius: 12, overflow: 'hidden' }}>
            <div style={th}>Found</div>
            <div style={th}>Detected by</div>
            <div style={th}>Replaced with</div>
            <div style={th}>Where</div>
            {sel.findings.map((f, i) => (
              <div key={i} style={{ display: 'contents' }}>
                <div style={{ ...td, fontSize: 13.5, fontWeight: 600 }}>{f.kind}</div>
                <div style={{ ...td, color: '#4A4458' }}>{f.method}</div>
                <div style={{ ...td, ...mono, color: '#5B21B6' }}>{f.token}</div>
                <div style={{ ...td, color: '#6B6580' }}>{f.where}</div>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 13, lineHeight: 1.65, color: '#4A4458', background: '#FAF9FD', borderRadius: 10, padding: '14px 16px' }}>{sel.note}</div>
        </div>
      </div>
    </div>
  );
}
