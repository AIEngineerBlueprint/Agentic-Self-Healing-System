import { useEffect, useMemo, useRef, useState } from 'react'

const CTRL = import.meta.env.VITE_CTRL_BASE || 'http://localhost:8090'
const PRODUCT = import.meta.env.VITE_PRODUCT_BASE || 'http://localhost:3000'

const STAGES = [
  ['TRIAGED', 'detect'],
  ['EVIDENCE_READY', 'evidence'],
  ['DIAGNOSIS_READY', 'attribute'],
  ['ROUTED', 'route'],
  ['PLAN_READY', 'plan'],
  ['POLICY_APPROVED', 'policy'],
  ['EXECUTING', 'patch'],
  ['VALIDATING', 'validate'],
  ['RESOLVED', 'resolved'],
]
const ORDER = STAGES.map(([s]) => s)
const TERMINAL_BAD = ['ROLLBACK', 'ESCALATED']

const ACTOR_CLASS = {
  'attribution-engine': 'attr',
  'diagnosis-agent': 'llm',
  'product-repair-agent': 'llm',
  'policy-engine': 'pol',
  'test-gate': 'test',
  'validation-agent': 'val',
}

function toneOf(entry) {
  const s = entry.summary || ''
  if (entry.kind === 'llm_call') return 'model'
  if (/DENIED|FAIL|ROLLBACK|ESCALATED|BLOCKED/.test(s)) return 'bad'
  if (/EXONERATED|PASS|RESOLVED|OK /.test(s)) return 'good'
  if (/REVIEW|refus|protected/i.test(s)) return 'warn'
  return null
}

// The decision feed was a flat firehose -- 30+ entries with no structure, which
// is where most of the dashboard's noise came from. Grouping by PHASE gives the
// same shape as the artifacts panel: a header, a one-line verdict, and detail
// only when you ask for it.
const PHASE_OF = {
  detector: 'Detect',
  'evidence-builder': 'Evidence',
  'attribution-engine': 'Attribute',
  'diagnosis-agent': 'Diagnose',
  router: 'Route',
  'product-repair-agent': 'Repair',
  'capability-repair-agent': 'Repair',
  'policy-engine': 'Policy',
  'test-gate': 'Test gate',
  'validation-agent': 'Validate',
  'tool-gateway': 'Guardrails',
}
const PHASE_ORDER = ['Detect', 'Evidence', 'Attribute', 'Diagnose', 'Route',
                     'Policy', 'Repair', 'Test gate', 'Validate', 'Guardrails']

function groupByPhase(events) {
  const byPhase = new Map()
  for (const e of events) {
    const phase = PHASE_OF[e.actor] || 'Other'
    if (!byPhase.has(phase)) byPhase.set(phase, [])
    byPhase.get(phase).push(e)
  }
  return PHASE_ORDER.filter((p) => byPhase.has(p)).map((p) => {
    const items = byPhase.get(p)
    const bad = items.some((e) => toneOf(e) === 'bad')
    const model = items.some((e) => e.kind === 'llm_call')
    // The last state transition in a phase is its verdict; otherwise the last entry.
    const verdict = [...items].reverse().find((e) => e.kind === 'state_transition') || items[items.length - 1]
    return { phase: p, items, bad, model, verdict }
  })
}

const shortId = (id) => (id || '').replace(/^inc-\d{4}-\d{2}-\d{2}-/, '')

/* The ASHS mark, redrawn as vector geometry rather than shipped as the source
 * PNG: it renders crisply at 28px in the header and at any size elsewhere,
 * costs a few hundred bytes instead of a megabyte, and needs no opaque
 * rectangle fighting the header background.
 *
 * Reduced to what survives at 28px -- shield, robot, one green recovery loop.
 * The source art's twin circular arrows become illegible at this size and were
 * dropped rather than kept as noise. */
function Mark({ size = 28 }) {
  return (
    <svg className="bar__mark" width={size} height={size} viewBox="0 0 48 48"
         fill="none" role="img" aria-label="ASHS">
      <defs>
        <linearGradient id="ashsMark" x1="6" y1="4" x2="42" y2="45" gradientUnits="userSpaceOnUse">
          <stop stopColor="#2f81f7" /><stop offset="1" stopColor="#3fb950" />
        </linearGradient>
      </defs>
      <path d="M24 3.5 41 10.2v14.4c0 8.9-7.3 15.6-17 20.1-9.7-4.5-17-11.2-17-20.1V10.2z"
            fill="url(#ashsMark)" fillOpacity=".15" stroke="url(#ashsMark)"
            strokeWidth="2.2" strokeLinejoin="round" />
      <path d="M14.2 32a10.4 10.4 0 0 0 19.6 0" stroke="#3fb950" strokeWidth="2.3"
            strokeLinecap="round" fill="none" />
      <path d="M14.9 27.2 14 32.4l5.2 1" stroke="#3fb950" strokeWidth="2.3"
            strokeLinecap="round" strokeLinejoin="round" fill="none" />
      <rect x="16" y="15.5" width="16" height="12.5" rx="4.5" fill="#0d1117"
            stroke="url(#ashsMark)" strokeWidth="1.8" />
      <path d="M24 15.5v-2.8" stroke="url(#ashsMark)" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="24" cy="11.4" r="1.7" fill="#3fb950" />
      <circle cx="20.7" cy="21.7" r="1.9" fill="#56d4dd" />
      <circle cx="27.3" cy="21.7" r="1.9" fill="#56d4dd" />
    </svg>
  )
}

export default function App() {
  const [incidents, setIncidents] = useState([])
  const [eventsById, setEventsById] = useState({})
  const [health, setHealth] = useState([])
  const [journey, setJourney] = useState([])
  const [config, setConfig] = useState({})
  const [connected, setConnected] = useState(false)
  const [selectedId, setSelectedId] = useState(null)
  const [pinned, setPinned] = useState(false)   // user picked one explicitly
  const [detail, setDetail] = useState(null)
  const [artifacts, setArtifacts] = useState(null)
  const [escalations, setEscalations] = useState([])
  const [reportFor, setReportFor] = useState(null)
  const [report, setReport] = useState(null)
  const [copied, setCopied] = useState(false)
  const [catalog, setCatalog] = useState(null)
  const [openPhase, setOpenPhase] = useState({})
  const [open, setOpen] = useState({ narrative: false, exonerated: true, signals: false, plan: false, diff: true, gate: true, refusals: true })
  const feedRef = useRef(null)

  useEffect(() => {
    fetch(`${CTRL}/api/catalog`).then((r) => r.json()).then(setCatalog).catch(() => {})
  }, [])

  // --- live feed ---------------------------------------------------------
  useEffect(() => {
    const es = new EventSource(`${CTRL}/api/stream`)
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    es.onmessage = (msg) => {
      const d = JSON.parse(msg.data)
      setIncidents(d.incidents || [])
      setHealth(d.health || [])
      setJourney(d.journey || [])
      setConfig(d.config || {})
      if (d.audit?.length) {
        setEventsById((prev) => {
          const next = { ...prev }
          for (const g of d.audit) {
            next[g.incident_id] = [...(next[g.incident_id] || []), ...g.entries].slice(-400)
          }
          return next
        })
      }
      // Follow the newest incident until the operator pins one.
      if (!pinned && d.incidents?.length) setSelectedId(d.incidents[0].incident_id)
    }
    return () => es.close()
  }, [pinned])

  const selected = incidents.find((i) => i.incident_id === selectedId) || incidents[0]
  const events = eventsById[selected?.incident_id] || []

  useEffect(() => {
    if (!selected) return
    fetch(`${CTRL}/api/incidents/${selected.incident_id}`)
      .then((r) => r.json()).then(setDetail).catch(() => {})
    fetch(`${CTRL}/api/incidents/${selected.incident_id}/artifacts`)
      .then((r) => r.json()).then(setArtifacts).catch(() => {})
    fetch(`${CTRL}/api/escalations`)
      .then((r) => r.json()).then((d) => setEscalations(d.escalations || [])).catch(() => {})
  }, [selected?.incident_id, selected?.state])

  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [events.length, selected?.incident_id])

  const journeyStats = useMemo(() => {
    const all = journey.flatMap((g) => g.ticks || [])
    const ok = all.filter((t) => t.ok).length
    const withTraffic = journey.filter((g) => (g.ticks || []).length > 0)
    return {
      ok, total: all.length,
      monitored: journey.length,
      active: withTraffic.length,
      healthy: all.length > 0 && ok === all.length,
    }
  }, [journey])

  const inc = detail?.incident
  const diag = inc?.diagnosis
  const narrative = diag?.narrative
  const m = artifacts?.metrics || {}

  // The escalation queue used to be a third panel stacked in the left column,
  // repeating incident ids that were already on screen a few rows above. An
  // escalation is a property OF an incident, so it belongs on that incident's
  // row -- a badge in the list, the full reason once you select it.
  const escById = useMemo(
    () => Object.fromEntries(escalations.map((e) => [e.incident_id, e])),
    [escalations],
  )
  const selectedEsc = selected ? escById[selected.incident_id] : null
  const exonerated = diag?.exonerated || []

  useEffect(() => {
    if (!reportFor) { setReport(null); return }
    setReport(null); setCopied(false)
    fetch(`${CTRL}/api/incidents/${reportFor}/report`)
      .then((r) => r.json()).then(setReport)
      .catch(() => setReport({ error: 'could not load the report' }))
  }, [reportFor])

  return (
    <>
      <header className="bar">
        <div className="bar__brand">
          <Mark />
          <span className="bar__name">ASHS</span>
          <span className="bar__sub">Agentic Self-Healing System</span>
        </div>
        <span className={`chip ${connected ? 'chip--live' : 'chip--halt'}`}>
          <span className={`dot ${connected ? 'dot--pulse' : ''}`} />
          {connected ? 'live' : 'disconnected'}
        </span>
        <span className={`chip ${config.kill_switch ? 'chip--halt' : ''}`}>
          kill switch <b>{config.kill_switch ? 'ENGAGED' : 'off'}</b>
        </span>
        <span className={`chip ${config.auto_execute ? 'chip--live' : 'chip--plan'}`}
              title="Global switch. Autonomy LEVEL is per-service, set in services.yaml.">
          execution <b>{config.auto_execute ? 'enabled' : 'paused'}</b>
        </span>
      </header>

      <div className="grid3">
        {/* ============================================ column 1: incidents */}
        <section className="panel">
          <div className="panel__head">
            <h2 className="panel__title">Incidents</h2>
            <span className="panel__note">
              {pinned ? (
                <a className="plain" onClick={() => setPinned(false)}>following latest</a>
              ) : 'auto'}
            </span>
          </div>
          <div className="panel__body panel__body--flush">
            {incidents.length === 0 && (
              <div className="empty">
                No incidents.<br />Run <code>make demo-scenario-1</code>.
              </div>
            )}
            {incidents.map((i) => (
              <IncidentRow
                key={i.incident_id}
                inc={i}
                esc={escById[i.incident_id]}
                active={i.incident_id === selected?.incident_id}
                onSelect={() => { setSelectedId(i.incident_id); setPinned(true) }}
              />
            ))}
          </div>

          <div className="panel__head" style={{ borderTop: '1px solid var(--line)' }}>
            <h2 className="panel__title">Product journeys</h2>
            <span className={`panel__note ${journeyStats.healthy ? 'ok' : 'bad'}`}>
              {journeyStats.total ? (journeyStats.healthy ? 'healthy' : 'DEGRADED') : 'no traffic'}
            </span>
          </div>
          <div className="panel__body" style={{ flex: 'none' }}>
            {journey.length === 0 && (
              <div className="journey__none">no entry points declared in the catalog</div>
            )}
            {journey.map((g) => {
              const ticks = g.ticks || []
              const ok = ticks.filter((t) => t.ok).length
              const avg = ticks.length ? ticks.reduce((a, t) => a + t.ms, 0) / ticks.length : 0
              const idle = ticks.length === 0
              return (
                <div className="jrow" key={`${g.service}-${g.entry_point}`}>
                  <div className="jrow__top">
                    <span className="jrow__name">{g.journey || g.entry_point}</span>
                    <span className={`jrow__badge ${idle ? 'is-idle' : ok === ticks.length ? 'is-ok' : 'is-bad'}`}>
                      {idle ? 'idle' : ok === ticks.length ? 'healthy' : `${ticks.length - ok} err`}
                    </span>
                  </div>
                  <div className="jrow__svc">
                    {g.product} · {g.service_name}
                  </div>
                  <div className="journey">
                    {ticks.map((t, n) => (
                      <span key={n} className={`tick ${t.ok ? 'tick--ok' : 'tick--err'}`}
                            title={`${t.ok ? 'ok' : 'ERROR'} · ${t.ms.toFixed(0)}ms`} />
                    ))}
                    {idle && <span className="journey__none">no traffic in the last 3 minutes</span>}
                  </div>
                  <div className="jrow__stats">
                    <code>{g.entry_point}</code>
                    {!idle && <span>{ok}/{ticks.length} ok · {avg.toFixed(0)} ms</span>}
                  </div>
                </div>
              )
            })}
            <div className="journey__stats">
              <span>{journeyStats.active}/{journeyStats.monitored} journeys with traffic</span>
              <a className="plain" href={PRODUCT} target="_blank" rel="noreferrer">open product ↗</a>
            </div>
            <div className="journey__note">
              Declared in <code>services.yaml</code> as <code>entry_points</code>; each tick is a
              real entry-point span, not a rendered page.
            </div>

            <div style={{ marginTop: 14 }}>
              {health.map((h) => {
                const req = Number(h.requests), err = Number(h.errors)
                const pct = req ? (err / req) * 100 : 0
                return (
                  <div className="svc" key={h.service_name}>
                    <span className="svc__name">{h.service_name}</span>
                    <div className="svc__track">
                      <span className="svc__ok" style={{ width: `${100 - pct}%` }} />
                      <span className="svc__err" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="svc__num">
                      {catalog?.services?.[h.service_name]?.autonomy_level && (
                        <span className={`lvl lvl--${catalog.services[h.service_name].autonomy_level}`}
                              title="Autonomy level from services.yaml">
                          {catalog.services[h.service_name].autonomy_level}
                        </span>
                      )}
                      {req}{err > 0 && <b> · {err}e</b>}
                    </span>
                  </div>
                )
              })}
            </div>
          </div>
        </section>

        {/* ========================================= column 2: the episode */}
        <section className="panel">
          <div className="panel__head">
            <h2 className="panel__title">Decision feed</h2>
            <span className="panel__note">
              {selected ? `${shortId(selected.incident_id)} · ${events.length} entries` : '—'}
            </span>
          </div>

          {selected && (
            <div className="pipeline-strip">
              <Pipeline state={selected.state} />
            </div>
          )}

          <div className="panel__body panel__body--flush feed" ref={feedRef}>
            {events.length === 0 && (
              <div className="empty">
                {selected ? 'Waiting for the next decision…' : 'Waiting for an incident.'}
              </div>
            )}
            {groupByPhase(events).map((g) => {
              const isOpen = openPhase[g.phase] ?? g.bad   // failures open by default
              return (
                <div className={`ph ${g.bad ? 'ph--bad' : ''}`} key={g.phase}>
                  <div className="ph__head"
                       onClick={() => setOpenPhase((o) => ({ ...o, [g.phase]: !isOpen }))}>
                    <span className="ph__caret">{isOpen ? '▾' : '▸'}</span>
                    <span className="ph__name">{g.phase}</span>
                    {g.model && <span className="ph__model">model</span>}
                    <span className="ph__count">{g.items.length}</span>
                  </div>
                  <div className="ph__verdict">{g.verdict.summary.replace(/^-> /, '')}</div>
                  {isOpen && (
                    <div className="ph__items">
                      {g.items.map((e, i) => (
                        <div key={`${e.seq}-${i}`}
                             className={`ev ${toneOf(e) ? `ev--${toneOf(e)}` : ''}`}>
                          <span className="ev__seq">{e.seq}</span>
                          <div>
                            <span className="ev__text">{e.summary.replace(/^-> /, '')}</span>
                            <Meta entry={e} />
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>

        {/* ======================================= column 3: the artifacts */}
        <section className="panel">
          <div className="panel__head">
            <h2 className="panel__title">Evidence &amp; artifacts</h2>
            <span className="panel__note">{shortId(inc?.incident_id) || '—'}</span>
          </div>
          <div className="panel__body">
            {!inc && <div className="empty">Select an incident.</div>}

            {inc && (
              <div className="stats" style={{ marginBottom: 12 }}>
                <Stat label="fault domain" value={inc.fault_domain || '—'} />
                <Stat
                  label="confidence"
                  value={inc.confidence ? Number(inc.confidence).toFixed(2) : '—'}
                  tone={inc.confidence >= 0.85 ? 'green' : inc.confidence ? 'amber' : null}
                />
                {/* Where it BROKE vs who OWNS it. When they match, that is a
                    finding -- the consumer was tested and kept -- not a
                    tautology, so say which it is instead of printing the same
                    value twice and leaving the viewer to guess. */}
                <Stat
                  label={inc.surfaced_in === inc.fault_domain
                    ? 'surfaced in — same owner' : 'surfaced in — ROUTED PAST'}
                  value={inc.surfaced_in || '—'}
                  tone={inc.surfaced_in && inc.surfaced_in !== inc.fault_domain ? 'amber' : null}
                />
                <Stat
                  label="exonerated"
                  value={exonerated.length ? `${exonerated.length} cleared` : '—'}
                  tone={exonerated.length ? 'green' : null}
                />
              </div>
            )}

            {inc && (m.ttr_seconds || m.execution_seconds) && (
              <div className="timings">
                <Timing label="detect → repaired" v={m.ttr_seconds} unit="s" big />
                <Timing label="execution" v={m.execution_seconds} unit="s" />
                <Timing label="occurrences" v={inc.occurrence_count} />
              </div>
            )}

            {/* ---- ESCALATION: the queue entry for THIS incident, in full ---- */}
            {selectedEsc && (
              <Art title="escalated — awaiting a human"
                   tag={selectedEsc.acknowledged ? 'acknowledged' : 'unacknowledged'}
                   open={open.escalation ?? true}
                   onToggle={() => setOpen((o) => ({ ...o, escalation: !(o.escalation ?? true) }))}>
                <div className="esc__why">{selectedEsc.reason}</div>
                <dl className="kv" style={{ marginTop: 8 }}>
                  <dt>owner</dt>
                  <dd>{selectedEsc.owner_team || selectedEsc.fault_domain || '—'}</dd>
                  {selectedEsc.owner_contact && (
                    <>
                      <dt>contact</dt><dd>{selectedEsc.owner_contact}</dd>
                    </>
                  )}
                  <dt>raised</dt>
                  <dd>{new Date(selectedEsc.raised_at).toLocaleTimeString()}</dd>
                </dl>
                {/* Telling someone they own an incident is not a handover. This
                    is the briefing: why it is theirs, what was tried, why it
                    stopped, and what to do — ready to paste into a ticket. */}
                <a className="plain" style={{ display: 'inline-block', marginTop: 10 }}
                   onClick={() => setReportFor(inc.incident_id)}>
                  Open the action report ↗
                </a>
              </Art>
            )}

            {/* ---- REFUSALS: the guardrails, made visible ---- */}
            {artifacts?.refusals?.length > 0 && (
              <Art title="refusals — guardrails that fired"
                   tag={`${artifacts.refusals.length} blocked`}
                   open={open.refusals}
                   onToggle={() => setOpen((o) => ({ ...o, refusals: !o.refusals }))}>
                {artifacts.refusals.map((r) => (
                  <div className="refusal" key={r.seq}>
                    <div className="refusal__head">
                      <code>{r.tool}</code>
                      <span className="refusal__actor">{r.actor}</span>
                    </div>
                    <div className="refusal__why">{r.reason}</div>
                  </div>
                ))}
              </Art>
            )}

            {/* ---- THE PATCH: the most concrete artifact in the system ---- */}
            {artifacts?.patch && (
              <Art title="the patch"
                   tag={`+${artifacts.patch.lines_added} / −${artifacts.patch.lines_removed}`}
                   open={open.diff}
                   onToggle={() => setOpen((o) => ({ ...o, diff: !o.diff }))}>
                <div className="diff__path"><code>{artifacts.patch.path}</code></div>
                <pre className="code diff">
                  {(artifacts.patch.diff || '').split('\n').map((l, i) => (
                    <div key={i}
                         className={l.startsWith('+') && !l.startsWith('+++') ? 'add'
                                  : l.startsWith('-') && !l.startsWith('---') ? 'del'
                                  : l.startsWith('@@') ? 'hunk' : ''}>{l || ' '}</div>
                  ))}
                </pre>
              </Art>
            )}

            {/* ---- TEST GATE: red before, green after ---- */}
            {artifacts?.test_gate?.length > 0 && (
              <Art title="test gate" tag="must fail, then pass"
                   open={open.gate}
                   onToggle={() => setOpen((o) => ({ ...o, gate: !o.gate }))}>
                {artifacts.test_gate.map((g, i) => {
                  const correct = (g.phase === 'pre_patch' && !g.passed) ||
                                  (g.phase === 'post_patch' && g.passed)
                  return (
                    <div key={i} className={`gate ${correct ? 'gate--ok' : 'gate--bad'}`}>
                      <div className="gate__head">
                        <b>{g.phase.replace('_', '-')}</b>
                        <span>{g.passed ? 'PASSED' : 'FAILED'}</span>
                        <em>{g.phase === 'pre_patch' ? 'must fail' : 'must pass'}</em>
                      </div>
                      <pre className="code">{(g.output || '').split('\n').slice(-8).join('\n')}</pre>
                    </div>
                  )
                })}
              </Art>
            )}

            {diag?.exonerated?.length > 0 && (
              <Art title="exonerated on objective evidence"
                   tag={`${diag.exonerated.length} capabilities`}
                   open={open.exonerated}
                   onToggle={() => setOpen((o) => ({ ...o, exonerated: !o.exonerated }))}>
                {diag.exonerated.map((x) => (
                  <div className="exon" key={x.repository}>
                    <b>{x.repository}</b> — {x.reason}
                  </div>
                ))}
              </Art>
            )}

            {narrative && (
              <Art title="root-cause narrative"
                   tag={narrative._meta?.degraded ? 'deterministic (degraded)' : narrative._meta?.model}
                   open={open.narrative}
                   onToggle={() => setOpen((o) => ({ ...o, narrative: !o.narrative }))}>
                <dl className="kv">
                  <dt>root cause</dt><dd>{narrative.root_cause}</dd>
                  <dt>why this owner</dt><dd>{narrative.why_this_owner}</dd>
                  <dt>fix strategy</dt><dd>{narrative.fix_strategy}</dd>
                  <dt>target</dt><dd><code>{narrative.target_file} :: {narrative.target_function}</code></dd>
                  <dt>risk</dt><dd>{narrative.risk}</dd>
                </dl>
              </Art>
            )}

            {diag?.signals?.length > 0 && (
              <Art title="attribution signals (deterministic)"
                   tag={`${diag.signals.length} signals`}
                   open={open.signals}
                   onToggle={() => setOpen((o) => ({ ...o, signals: !o.signals }))}>
                {diag.signals.map((s, i) => (
                  <div key={i} style={{ marginBottom: 7 }}>
                    <code style={{ color: 'var(--blue)' }}>{s.name}</code>{' '}
                    <span style={{ color: 'var(--ink-faint)' }}>w={s.weight}{s.conclusive ? ' · conclusive' : ''}</span>
                    <div style={{ color: 'var(--ink-mute)' }}>{s.verdict}</div>
                  </div>
                ))}
              </Art>
            )}

            {inc?.plan && (
              <Art title="repair plan" tag={inc.plan.action_type}
                   open={open.plan}
                   onToggle={() => setOpen((o) => ({ ...o, plan: !o.plan }))}>
                <dl className="kv">
                  <dt>agent</dt><dd>{inc.plan.repair_agent}</dd>
                  <dt>targets</dt>
                  <dd>{(inc.plan.target_paths || []).map((p) => <div key={p}><code>{p}</code></div>)}</dd>
                  <dt>risk</dt><dd>{inc.plan.risk}</dd>
                  <dt>test gate</dt>
                  <dd>{inc.plan.test_spec?.must_fail_pre_patch
                    ? 'must fail pre-patch, pass post-patch' : '—'}</dd>
                  <dt>rollback</dt><dd>{inc.plan.rollback?.strategy}</dd>
                </dl>
              </Art>
            )}
          </div>
        </section>
      </div>

      {reportFor && (
        <ActionReport
          report={report}
          copied={copied}
          onCopy={() => {
            navigator.clipboard?.writeText(report?.markdown || '')
            setCopied(true)
          }}
          onClose={() => setReportFor(null)}
        />
      )}
    </>
  )
}

/* The handover an escalation should have been all along: why it is yours, what
 * was tried, why it stopped, and what to do — with a markdown copy for the
 * ticket, because the next thing anyone does is paste it somewhere. */
function ActionReport({ report, copied, onCopy, onClose }) {
  if (!report) {
    return (
      <div className="sheet" onClick={onClose}>
        <div className="sheet__panel"><div className="empty">Assembling the report…</div></div>
      </div>
    )
  }
  const w = report.what_broke || {}
  const y = report.why_it_is_yours || {}
  const t = report.what_we_tried || {}

  return (
    <div className="sheet" onClick={onClose}>
      <div className="sheet__panel" onClick={(e) => e.stopPropagation()}>
        <div className="sheet__head">
          <div>
            <div className="sheet__title">Action report</div>
            <div className="sheet__sub">
              {report.incident_id} · {report.owner?.team || report.owner?.repository}
              {report.owner?.contact ? ` · ${report.owner.contact}` : ''}
            </div>
          </div>
          <button className="btn" onClick={onCopy}>
            {copied ? 'copied ✓' : 'copy as markdown'}
          </button>
          <button className="btn btn--ghost" onClick={onClose}>close</button>
        </div>

        <div className="sheet__body">
          <div className="rep__block rep__block--stop">
            <div className="rep__label">why we stopped</div>
            <div className="rep__stop">{report.why_we_stopped || '—'}</div>
          </div>

          <div className="rep__label">what to do next</div>
          <ol className="rep__steps">
            {(report.what_to_do_next || []).map((s, i) => <li key={i}>{s}</li>)}
          </ol>

          <div className="rep__label">what broke</div>
          <dl className="kv">
            <dt>entry point</dt><dd><code>{w.entry_point}</code></dd>
            <dt>occurrences</dt><dd>{w.occurrences} since {String(w.first_seen).slice(11, 19)}</dd>
            <dt>surfaced in</dt><dd>{w.surfaced_in}</dd>
          </dl>

          <div className="rep__label">why it is yours</div>
          <dl className="kv">
            <dt>fault domain</dt>
            <dd><b>{y.fault_domain}</b> at {Number(y.confidence || 0).toFixed(2)}
              {y.routed_past && <span className="rep__tag">routed past {w.surfaced_in}</span>}
            </dd>
          </dl>
          {(y.signals || []).map((s, i) => (
            <div className="rep__sig" key={i}>
              <code>{s.name}</code> <span>{s.weight}</span>
              <div>{s.verdict}</div>
            </div>
          ))}
          {(y.exonerated || []).map((e, i) => (
            <div className="rep__sig rep__sig--ok" key={`x${i}`}>
              <code>ruled out {e.repository}</code>
              <div>{e.reason}</div>
            </div>
          ))}

          <div className="rep__label">what the system tried</div>
          <dl className="kv">
            <dt>plan</dt>
            <dd>{t.plan?.action || '—'} on {(t.plan?.targets || []).join(', ') || '—'}</dd>
            <dt>patch</dt>
            <dd>{t.patch_drafted
              ? (t.patch_applied ? 'drafted and applied' : 'drafted, NOT applied')
              : 'not drafted'}</dd>
          </dl>
          {(t.test_gate || []).map((g, i) => (
            <div key={i} className={`gate ${g.passed ? 'gate--ok' : 'gate--bad'}`}>
              <div className="gate__head"><b>{g.phase}</b>
                <em>{g.passed ? 'passed' : 'failed'}</em></div>
            </div>
          ))}
          {(t.policy_gates || []).filter((g) => g.passed === false).map((g, i) => (
            <div className="rep__sig" key={`g${i}`}>
              <code>{g.gate} refused</code><div>{g.detail}</div>
            </div>
          ))}

          {report.reproduce && (
            <>
              <div className="rep__label">reproduce it yourself</div>
              <pre className="code">{report.reproduce}</pre>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/* Per-incident row with its own compact pipeline. Clicking pins the drill-down. */
function IncidentRow({ inc, esc, active, onSelect }) {
  const bad = TERMINAL_BAD.includes(inc.state)
  const done = inc.state === 'RESOLVED'
  // A stale close is still RESOLVED, but nothing shipped. Saying so costs one
  // word and stops the row reading as a repair that never happened.
  const stale = done && inc.outcome === 'stale'
  const label = stale ? 'RESOLVED · stale' : inc.state
  return (
    <div className={`inc ${active ? 'inc--active' : ''}`} onClick={onSelect}>
      <div className="inc__top">
        <span className="inc__id">{shortId(inc.incident_id)}</span>
        {esc && !esc.acknowledged && (
          <span className="inc__esc" title={esc.reason}>
            awaiting {esc.owner_team || esc.fault_domain || 'a human'}
          </span>
        )}
        <span className={`inc__state ${stale ? 'is-stale' : done ? 'is-good' : bad ? 'is-bad' : 'is-run'}`}>
          {label}
        </span>
      </div>
      <div className="inc__meta">
        {inc.fault_domain || 'attributing…'}
        {inc.confidence ? ` · ${Number(inc.confidence).toFixed(2)}` : ''}
        {` · ${inc.occurrence_count}×`}
      </div>
      <MiniPipeline state={inc.state} />
    </div>
  )
}

function stageClass(state, name) {
  if (TERMINAL_BAD.includes(state)) {
    // Every stage up to the failure used to render red, so a single escalation
    // painted the whole strip. Those stages ran; the episode failed at the end.
    // 'past' says completed-but-not-a-success without spending the red.
    return ORDER.indexOf(name) <= ORDER.indexOf('VALIDATING') ? 'past' : ''
  }
  const at = ORDER.indexOf(state)
  const idx = ORDER.indexOf(name)
  if (idx < at) return 'done'
  if (idx === at) return state === 'RESOLVED' ? 'done' : 'active'
  return ''
}

function MiniPipeline({ state }) {
  return (
    <div className="mini">
      {STAGES.map(([name]) => (
        <span key={name} className={`mini__seg mini__seg--${stageClass(state, name) || 'todo'}`} />
      ))}
    </div>
  )
}

function Pipeline({ state }) {
  return (
    <div className="pipeline">
      {STAGES.map(([name, label], i) => (
        <div key={name} style={{ display: 'flex', alignItems: 'center' }}>
          {i > 0 && <span className="stage__sep" />}
          <span className={`stage ${stageClass(state, name) ? `stage--${stageClass(state, name)}` : ''}`}>
            {label}
          </span>
        </div>
      ))}
      {TERMINAL_BAD.includes(state) && (
        <>
          <span className="stage__sep" />
          <span className="stage stage--failed">{state.toLowerCase()}</span>
        </>
      )}
    </div>
  )
}

function Timing({ label, v, unit = '', big = false }) {
  if (v === undefined || v === null) return null
  return (
    <div className={`timing ${big ? 'timing--big' : ''}`}>
      <div className="timing__v">{typeof v === 'number' ? v.toFixed(v < 10 ? 1 : 0) : v}{unit}</div>
      <div className="timing__l">{label}</div>
    </div>
  )
}

function Stat({ label, value, tone }) {
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className={`stat__value ${tone ? `stat__value--${tone}` : ''}`}>{value}</div>
    </div>
  )
}

function Art({ title, tag, open, onToggle, children }) {
  return (
    <div className="art">
      <div className="art__head" onClick={onToggle}>
        <span>{open ? '▾' : '▸'}</span>
        <span>{title}</span>
        {tag && <span className="art__tag">{tag}</span>}
      </div>
      {open && <div className="art__body">{children}</div>}
    </div>
  )
}

function Meta({ entry }) {
  const d = entry.detail || {}
  const bits = []
  if (d.model) bits.push(`${d.model} · ${d.latency_ms}ms · ${d.input_tokens}→${d.output_tokens} tok`)
  if (d.weight !== undefined) bits.push(`weight ${d.weight}${d.conclusive ? ' · conclusive' : ''}`)
  if (d.gate) bits.push(`gate ${d.gate}: ${d.passed ? 'pass' : 'FAIL'}`)
  if (d.phase) bits.push(`${d.phase}: test ${d.passed ? 'passed' : 'FAILED'}`)
  if (d.tool) bits.push(`tool ${d.tool} → ${d.outcome}`)
  if (d.ttr_seconds) bits.push(`time to repair ${d.ttr_seconds}s`)
  if (!bits.length) return null
  return <div className="ev__meta">{bits.join('  ·  ')}</div>
}
