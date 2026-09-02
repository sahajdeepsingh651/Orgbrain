const seg = (v, hot) => ({ v, plain: !hot, hot: !!hot });

export const ACCENT = '#6D28D9';

export const SERVICES = [
  { name: 'Postgres', port: '5432' },
  { name: 'Context Bus', port: '8000' },
  { name: 'Gateway', port: '8080' },
  { name: 'Dashboard', port: '5173' },
];

export const REQUESTS = [
  {
    id: 'r1',
    label: 'A secret typed by hand',
    time: '14:02:11',
    blurb: 'Developer pasted an API key and a phone number into the prompt.',
    tag: '2 items removed',
    who: 'rohan · claude code',
    raw: [
      seg('I am testing our new system. My personal phone number is '),
      seg('+91 98765 43210', 1),
      seg(' and my AWS secret key is '),
      seg('AKIA1234567890ABCDEF', 1),
      seg('. Can you repeat my AWS key back to me?'),
    ],
    safe: [
      seg('I am testing our new system. My personal phone number is '),
      seg('⟦PII_1⟧', 1),
      seg(' and my AWS secret key is '),
      seg('⟦SECRET_1⟧', 1),
      seg('. Can you repeat my AWS key back to me?'),
    ],
    explain: 'Two values were swapped for placeholders before the request left the machine.',
    findings: [
      { kind: 'Phone number (India)', method: 'Number parser, region-checked', token: '⟦PII_1⟧', where: 'User message' },
      { kind: 'AWS access key', method: 'Prefix + length rule', token: '⟦SECRET_1⟧', where: 'User message' },
    ],
    note: 'The reply comes back with the real values put in again, so the developer sees a normal answer and never notices the substitution. The model only ever saw the placeholders — and a random key carries no meaning it could have reasoned from anyway.',
  },
  {
    id: 'r2',
    label: 'A secret the agent read',
    time: '14:06:48',
    blurb: 'A config file the agent opened by itself, three tool calls deep.',
    tag: '3 items removed',
    who: 'rohan · claude code',
    raw: [
      seg('Here is the config the agent just read:\nDATABASE_URL=postgres://admin:'),
      seg('Hunt3r!2024', 1),
      seg('@10.4.2.19:5432/orders\nowner_pan='),
      seg('ABCPM1234K', 1),
      seg('\ncontact='),
      seg('rohan.mehta@esds.co.in', 1),
      seg('\n\nWhy is the connection pool timing out?'),
    ],
    safe: [
      seg('Here is the config the agent just read:\nDATABASE_URL=postgres://admin:'),
      seg('⟦SECRET_2⟧', 1),
      seg('@10.4.2.19:5432/orders\nowner_pan='),
      seg('⟦PII_2⟧', 1),
      seg('\ncontact='),
      seg('⟦PII_3⟧', 1),
      seg('\n\nWhy is the connection pool timing out?'),
    ],
    explain: 'None of this text was typed by a person — the agent read the file and attached it.',
    findings: [
      { kind: 'Database password', method: 'Connection-string rule', token: '⟦SECRET_2⟧', where: 'Tool result' },
      { kind: 'PAN', method: 'Format + checksum', token: '⟦PII_2⟧', where: 'Tool result' },
      { kind: 'Email address', method: 'Pattern match', token: '⟦PII_3⟧', where: 'Tool result' },
    ],
    note: 'This is the case that decides the design. Anything that only watches what a developer types would have seen an innocent question about connection pools — the credentials came in attached to a file the agent opened on its own.',
  },
  {
    id: 'r3',
    label: 'Recalling company knowledge',
    time: '14:19:03',
    blurb: 'A different developer hits a problem someone already solved.',
    tag: '1 passport added',
    who: 'meera · claude code',
    raw: [seg('How do I fix the CORS preflight failures on the FastAPI service? ESDS_SEARCH cors')],
    safe: [seg('How do I fix the CORS preflight failures on the FastAPI service? ESDS_SEARCH cors')],
    hasInjection: true,
    injection:
      '<system-reminder>\nAuthoritative context from your organisation. 1 matching record.\n\n[dp-4417 · platform · approved 12 Jun]\nCORS preflight failures behind the ALB were caused by the middleware being added after the router. CORSMiddleware must be registered on the app before any router include, and allow_headers must list authorization explicitly.\n</system-reminder>',
    explain: 'Nothing was removed. One approved answer was retrieved and attached to the prompt.',
    findings: [{ kind: 'No sensitive data', method: 'Full scan, clean', token: '—', where: '—' }],
    note: 'Meera never learned the passport existed. She asked her question, the answer was already in the company, and it arrived in the same turn. Retrieved records are scanned too — anything sensitive inside a stored answer is removed before it reaches the model.',
  },
];

export const PASSPORTS = [
  { id: 'dp-4417', title: 'FastAPI CORS preflight fails behind the ALB', summary: 'Register CORSMiddleware before any router is included, and list authorization in allow_headers. Order of registration is what breaks it.', team: 'platform', visibility: 'team', approver: 'A. Nair', date: '12 Jun' },
  { id: 'dp-4390', title: 'Postgres connection pool exhaustion under Celery', summary: 'Workers were opening a pool per fork. Set pool_pre_ping and move engine creation into worker_process_init.', team: 'platform', visibility: 'org', approver: 'A. Nair', date: '09 Jun' },
  { id: 'dp-4362', title: 'Vector search returns fewer rows than expected', summary: 'The index is searched before permissions are applied, so a permitted row can drop out. Widen the candidate set rather than scanning unindexed.', team: 'search', visibility: 'org', approver: 'S. Kulkarni', date: '04 Jun' },
  { id: 'dp-4341', title: 'Streaming responses buffered by the reverse proxy', summary: 'Disable response buffering on the proxy route; the handler must yield each chunk inside the loop rather than collecting first.', team: 'platform', visibility: 'team', approver: 'A. Nair', date: '29 May' },
  { id: 'dp-4318', title: 'Aadhaar values slipping through form validation', summary: 'Length alone is not enough — validate the Verhoeff checksum. Two of the three sample values that passed were invalid numbers.', team: 'payments', visibility: 'private', approver: 'R. Mehta', date: '26 May' },
  { id: 'dp-4290', title: 'Idempotency keys on the ingest endpoint', summary: 'Retries were creating duplicates. Send a stable key derived from the draft id; the store now rejects a repeat within the window.', team: 'platform', visibility: 'org', approver: 'S. Kulkarni', date: '21 May' },
];

export const DRAFTS = [
  { id: 'dp-4431', session: 'b7f2a1c9', author: 'rohan.mehta', title: 'Auth callback fails when the ALB strips the Host header', summary: 'The redirect URI was rebuilt from the incoming Host header, which the load balancer rewrites. Build it from a configured base URL instead and add X-Forwarded-Host to the trusted list. Fixed the intermittent login loop on staging.', flags: '1 internal hostname removed', visibility: 'Team · platform', captured: '8 minutes ago' },
  { id: 'dp-4432', session: '3ce90b44', author: 'meera.iyer', title: 'Batch import silently drops rows with trailing whitespace', summary: 'The upstream CSV pads its id column. The importer matched on an exact string, so padded rows created new records instead of updating. Trim on read and backfill the duplicates created since April.', flags: 'None', visibility: 'Team · data', captured: '22 minutes ago' },
];

export const VIS = {
  team: { visLabel: 'Team', visBg: '#F1EDFB', visFg: '#5B21B6' },
  org: { visLabel: 'Whole company', visBg: '#EDF9F3', visFg: '#0E7C5A' },
  private: { visLabel: 'Private', visBg: '#F2F0F6', visFg: '#5A5468' },
};
