import { useEffect, useRef, useState } from 'react';
import './App.css';
import Overview from './components/Overview';
import XRayMonitor from './components/XRayMonitor';
import ContextBusExplorer from './components/ContextBusExplorer';
import ApprovalInbox from './components/ApprovalInbox';
import { PASSPORTS, DRAFTS, SERVICES } from './data';

const TITLES = {
  home: ['Overview', 'What Orgbrain does, in the order it happens.'],
  xray: ['Live X-Ray', 'The same request before and after the checkpoint.'],
  bus: ['Context Bus', 'Approved answers the company can retrieve.'],
  inbox: ['Approval Inbox', 'Drafts that need a person before they are stored.'],
};

const NAV = [
  { key: 'home', icon: '◆', label: 'Overview' },
  { key: 'xray', icon: '◈', label: 'Live X-Ray' },
  { key: 'bus', icon: '▤', label: 'Context Bus' },
  { key: 'inbox', icon: '▣', label: 'Approval Inbox' },
];

export default function App() {
  const [tab, setTab] = useState('home');
  const [passports, setPassports] = useState(PASSPORTS);
  const [drafts, setDrafts] = useState(DRAFTS);
  const [hiddenDraftIds, setHiddenDraftIds] = useState(new Set());
  const [toast, setToast] = useState('');
  const timer = useRef(null);

  useEffect(() => {
    const fetchDrafts = async () => {
      try {
        const res = await fetch(`http://${window.location.hostname}:8080/v1/dashboard/pending`);
        const data = await res.json();
        
        setHiddenDraftIds(prevHidden => {
          const mapped = (data.drafts || [])
            .filter(d => !prevHidden.has(d.pending_id))
            .map(d => ({
              id: d.pending_id,
              session: d.session_id ? d.session_id.substring(0, 8) : 'unknown',
              author: d.account_uuid ? 'user' : 'unknown',
              title: (d.draft?.knowledge?.title) || `Draft ${d.pending_id}`,
              summary: (d.draft?.knowledge?.summary) || d.draft?.content || '',
              flags: d.sensitivity_flags?.redaction_count ? `${d.sensitivity_flags.redaction_count} items removed` : 'None',
              visibility: d.draft?.visibility || 'team',
              captured: new Date((d.created_at || Date.now()/1000) * 1000).toLocaleTimeString(),
              raw: d
            }));
          setDrafts(mapped);
          return prevHidden;
        });

        const mappedApproved = (data.approved_drafts || []).map(d => ({
          id: d.pending_id,
          title: (d.draft?.knowledge?.title) || `Draft ${d.pending_id}`,
          summary: (d.draft?.knowledge?.summary) || d.draft?.content || '',
          team: 'platform',
          visibility: d.draft?.visibility || 'team',
          approver: 'you',
          date: 'today'
        }));

        setPassports(prevPassports => {
          const all = [...mappedApproved];
          const unique = [];
          const seen = new Set();
          // Put mappedApproved at the top
          for (const p of all) {
            if (!seen.has(p.id)) {
              seen.add(p.id);
              unique.push(p);
            }
          }
          // Then append any existing passports (including mock data) that weren't in mappedApproved
          for (const p of prevPassports) {
            if (!seen.has(p.id)) {
              seen.add(p.id);
              unique.push(p);
            }
          }
          return unique;
        });

      } catch (e) {
        console.error(e);
      }
    };
    fetchDrafts();
    const interval = setInterval(fetchDrafts, 3000);
    return () => {
      clearInterval(interval);
      clearTimeout(timer.current);
    };
  }, []);

  const flash = (msg) => {
    setToast(msg);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setToast(''), 3200);
  };

  const approve = (d) => {
    setHiddenDraftIds(prev => new Set(prev).add(d.id));
    setDrafts((s) => s.filter((x) => x.id !== d.id));
    setPassports((s) => [
      { id: d.id, title: d.title, summary: d.summary, team: 'platform', visibility: d.visibility, approver: 'you', date: 'today' },
      ...s,
    ]);
    flash('Published to the Context Bus — ' + d.id + ' is now retrievable.');
  };

  const reject = (d) => {
    setHiddenDraftIds(prev => new Set(prev).add(d.id));
    setDrafts((s) => s.filter((x) => x.id !== d.id));
    flash('Discarded ' + d.id + '. Nothing was stored.');
  };

  const [title, sub] = TITLES[tab];

  return (
    <div style={{ minHeight: '100vh', display: 'grid', gridTemplateColumns: '264px 1fr', background: '#F6F5F9', color: '#16121F' }}>
      <aside style={{ background: '#FFFFFF', borderRight: '1px solid #E7E4EE', padding: '24px 16px', display: 'flex', flexDirection: 'column', gap: 28, position: 'sticky', top: 0, height: '100vh' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '0 8px' }}>
          <div style={{ width: 34, height: 34, borderRadius: 10, background: 'linear-gradient(150deg, #8B5CF6, #5B21B6)', display: 'flex', alignItems: 'center', justifyContent: 'center', boxShadow: '0 4px 10px rgba(91,33,182,0.28)' }}>
            <div style={{ width: 13, height: 13, border: '2.5px solid #fff', borderRadius: 4 }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 700, letterSpacing: '-0.02em' }}>Orgbrain</div>
            <div style={{ fontSize: 11, color: '#8A8398', letterSpacing: '0.04em', textTransform: 'uppercase' }}>Orgbrain</div>
          </div>
        </div>

        <nav style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: '#9A94A8', letterSpacing: '0.07em', textTransform: 'uppercase', padding: '0 10px 8px' }}>Dashboard</div>
          {NAV.map((n) => {
            const on = tab === n.key;
            return (
              <div key={n.key} className="ob-nav" onClick={() => setTab(n.key)} style={{ display: 'flex', alignItems: 'center', gap: 11, padding: '10px 12px', borderRadius: 9, cursor: 'pointer', fontSize: 14.5, fontWeight: 500, transition: 'background .15s', background: on ? '#F1EDFB' : 'transparent', color: on ? '#6D28D9' : '#4A4458' }}>
                <span style={{ width: 18, display: 'flex', justifyContent: 'center' }}>{n.icon}</span>
                <span style={{ flex: 1 }}>{n.label}</span>
                {n.key === 'inbox' && drafts.length > 0 && (
                  <span style={{ background: '#F5E6C8', color: '#8A5A05', fontSize: 11.5, fontWeight: 700, padding: '2px 8px', borderRadius: 99 }}>{drafts.length}</span>
                )}
              </div>
            );
          })}
        </nav>

        <div style={{ marginTop: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ border: '1px solid #E7E4EE', borderRadius: 12, padding: 14, background: '#FBFAFD', display: 'flex', flexDirection: 'column', gap: 9 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#9A94A8', letterSpacing: '0.07em', textTransform: 'uppercase' }}>Services</div>
            {SERVICES.map((svc) => (
              <div key={svc.name} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: '#4A4458' }}>
                <span style={{ width: 7, height: 7, borderRadius: 99, background: '#17A673', boxShadow: '0 0 0 3px rgba(23,166,115,0.15)' }} />
                <span style={{ flex: 1 }}>{svc.name}</span>
                <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 11, color: '#9A94A8' }}>{svc.port}</span>
              </div>
            ))}
          </div>
          <div style={{ fontSize: 11.5, color: '#9A94A8', padding: '0 4px', lineHeight: 1.5 }}>
            Showing sample data so the flow is visible without a live agent attached.
          </div>
        </div>
      </aside>

      <main style={{ minWidth: 0 }}>
        <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 24, padding: '20px 36px', borderBottom: '1px solid #E7E4EE', background: 'rgba(255,255,255,0.85)', backdropFilter: 'blur(8px)', position: 'sticky', top: 0, zIndex: 5 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <h1 style={{ margin: 0, fontSize: 20, fontWeight: 700, letterSpacing: '-0.02em' }}>{title}</h1>
            <div style={{ fontSize: 13.5, color: '#6B6580' }}>{sub}</div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, background: '#F1EDFB', color: '#5B21B6', border: '1px solid #E0D5F7', fontSize: 12.5, fontWeight: 600, padding: '6px 12px', borderRadius: 99 }}>
              <span style={{ width: 6, height: 6, borderRadius: 99, background: '#7C3AED' }} />
              Demo data
            </span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 7, background: '#FFFFFF', border: '1px solid #E7E4EE', color: '#4A4458', fontSize: 12.5, fontWeight: 500, padding: '6px 12px', borderRadius: 99, fontFamily: "'JetBrains Mono', monospace" }}>gateway :8080</span>
          </div>
        </header>

        <div style={{ padding: '32px 36px 56px' }}>
          {tab === 'home' && <Overview go={setTab} passportCount={passports.length} draftCount={drafts.length} />}
          {tab === 'xray' && <XRayMonitor />}
          {tab === 'bus' && <ContextBusExplorer passports={passports} />}
          {tab === 'inbox' && <ApprovalInbox drafts={drafts} onApprove={approve} onReject={reject} />}
        </div>
      </main>

      {toast && (
        <div style={{ position: 'fixed', bottom: 26, left: '50%', transform: 'translateX(-50%)', background: '#191428', color: '#F3EFFB', fontSize: 14, fontWeight: 500, padding: '13px 22px', borderRadius: 12, boxShadow: '0 12px 30px rgba(25,20,40,0.3)', zIndex: 50 }}>
          {toast}
        </div>
      )}
    </div>
  );
}
