import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import * as React from 'react'

const { useEffect, useRef, useState } = React

function h(type, props) {
  const children = Array.prototype.slice.call(arguments, 2)
  return React.createElement(type, props, children.length ? children : undefined)
}

const EXPECTED_PROTOCOL_VERSION = '2'
const PHASES = Object.freeze(['D1', 'D2', 'D3', 'D4', 'DEV1', 'DEV2', 'DEV3', 'DEV4', 'PC1'])
const CLOSED_TASK_STATUSES = Object.freeze(['done', 'completed', 'archived', 'cancelled'])
const POLL_MS = 30000

const styles = {
  page: { display: 'flex', flexDirection: 'column', gap: 12, padding: 16, width: '100%', maxWidth: '100%', minWidth: 0, boxSizing: 'border-box', overflow: 'hidden' },
  toolbar: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' },
  actions: { display: 'flex', gap: 8, flexWrap: 'wrap' },
  title: { margin: 0, fontSize: 18 },
  button: { cursor: 'pointer', padding: '4px 10px', border: '1px solid #cbd2d9', borderRadius: 4, background: 'var(--color-card, #ffffff)', color: 'var(--color-foreground, #1f2933)' },
  warn: { border: '1px solid #b45309', background: '#fff4e0', color: '#8a5a00', padding: 8, borderRadius: 4 },
  error: { border: '1px solid #b91c1c', background: '#fdecec', color: '#9b1c1c', padding: 8, borderRadius: 4 },
  info: { border: '1px solid #2563eb', background: '#e7f1ff', color: '#1a4f8b', padding: 8, borderRadius: 4 },
  columns: { display: 'flex', gap: 12, overflowX: 'auto', alignItems: 'flex-start', width: '100%', maxWidth: '100%', minWidth: 0, paddingBottom: 8 },
  column: { width: 240, maxWidth: 240, minWidth: 240, flex: '0 0 240px', border: '1px solid #d9e2ec', borderRadius: 8, padding: 8, boxSizing: 'border-box', background: 'var(--color-card, #ffffff)', color: 'var(--color-foreground, #1f2933)' },
  columnHeader: { display: 'flex', justifyContent: 'space-between', padding: '8px 0', marginBottom: 8, borderBottom: '1px solid #d9e2ec' },
  columnTitle: { margin: 0, fontSize: 14 },
  count: { fontSize: 12, color: 'var(--color-muted-foreground, #616e7c)' },
  empty: { fontSize: 12, color: 'var(--color-muted-foreground, #616e7c)', padding: 8 },
  card: { border: '1px solid #d9e2ec', borderRadius: 6, padding: 8, marginBottom: 6, background: 'var(--color-card, #fbfcfe)', color: 'var(--color-foreground, #1f2933)' },
  cardClickable: { cursor: 'pointer' },
  cardInvalid: { border: '1px solid #9b1c1c', background: '#fdecec', color: 'var(--color-foreground, #1f2933)' },
  cardTitle: { fontWeight: 600, fontSize: 13, marginBottom: 6 },
  cardMeta: { display: 'flex', flexWrap: 'wrap', gap: 4 },
  badge: { fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'var(--color-muted, #eef2f6)', color: 'var(--color-foreground, #3e4c59)' },
  badgePhase: { fontSize: 11, padding: '2px 6px', borderRadius: 4, background: '#e7f1ff', color: '#1a4f8b' },
  taskStatus: { fontSize: 11, padding: '2px 6px', borderRadius: 4, background: 'var(--color-muted, #f0f4f8)', color: 'var(--color-muted-foreground, #616e7c)' },
  diagnostics: { display: 'flex', flexDirection: 'column', gap: 6 },
  diagnostic: { fontSize: 12, padding: 6, borderRadius: 4, background: 'var(--color-muted, #f3f4f6)', color: 'var(--color-foreground, #1f2933)' },
  diagnosticWarn: { fontSize: 12, padding: 8, borderRadius: 4, background: '#fff4e0', color: '#8a5a00' },
  diagnosticError: { fontSize: 12, padding: 8, borderRadius: 4, background: '#fdecec', color: '#9b1c1c' },
  unpositioned: { fontSize: 12, color: '#9b1c1c' },
  detailOverlay: { position: 'fixed', inset: 0, background: 'rgba(15, 23, 32, 0.45)', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', padding: 24, overflow: 'auto', zIndex: 100 },
  detailPanel: { width: '100%', maxWidth: 640, background: 'var(--color-popover, var(--color-card, #ffffff))', color: 'var(--color-popover-foreground, var(--color-foreground, #1f2933))', border: '1px solid #d9e2ec', borderRadius: 8, display: 'flex', flexDirection: 'column', maxHeight: 'calc(100vh - 48px)' },
  detailHeader: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '12px 16px', borderBottom: '1px solid #d9e2ec' },
  detailTitle: { margin: 0, fontSize: 17 },
  detailClose: { cursor: 'pointer', padding: '4px 10px', border: '1px solid #cbd2d9', borderRadius: 6, background: 'var(--color-card, #ffffff)', color: 'var(--color-foreground, #1f2933)' },
  detailBody: { padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 12, overflow: 'auto' },
  detailSection: { display: 'flex', flexDirection: 'column', gap: 6 },
  detailSectionTitle: { margin: 0, fontSize: 14, color: 'var(--color-heading-foreground, var(--color-muted-foreground, #616e7c))' },
  cardBody: { fontSize: 14, lineHeight: 1.45, whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
  detailGroup: { border: '1px solid #d9e2ec', borderRadius: 6, padding: '6px 10px', background: 'var(--color-card, #fbfcfe)' },
  detailGroupSummary: { cursor: 'pointer', fontSize: 13, fontWeight: 600, color: 'var(--color-foreground, #1f2933)' },
  taskEntry: { display: 'flex', flexDirection: 'column', gap: 2, marginTop: 6 },
  attachments: { display: 'flex', flexDirection: 'column', gap: 2, paddingLeft: 12, marginTop: 2 },
  attachment: { fontSize: 12, color: 'var(--color-muted-foreground, #616e7c)' },
  diagnosticField: { fontSize: 12, color: 'var(--color-foreground, #1f2933)' }
}

// Normalize an authoritative exact step (e.g. "DEV3.1", "DEV1.1a") to its
// recognized top-level phase for DISPLAY only. Unknown values map to null.
function normalizePhase(raw) {
  if (raw == null) return null
  const p = String(raw).toUpperCase()
  const match = p.match(/^(D[1-4]|DEV[1-4]|PC1)(\.[0-9A-Z]+)?$/)
  if (match) return match[1]
  return null
}

function phaseOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.current_phase != null ? record.current_phase : record.lifecycle_phase
  if (raw == null) return null
  const p = String(raw).toUpperCase()
  if (PHASES.indexOf(p) >= 0) return p
  return normalizePhase(p)
}

function segmentOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.current_segment_id != null ? record.current_segment_id : record.segment_id
  return raw == null ? null : String(raw)
}

function nativeStatusOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.status != null ? record.status : record.native_status
  return raw == null ? null : String(raw)
}

function isTaskOpen(record) {
  if (!record || typeof record !== 'object') return false
  const status = nativeStatusOf(record)
  if (status == null) return true
  const s = status.toLowerCase()
  return CLOSED_TASK_STATUSES.indexOf(s) < 0
}

function titleOf(record) {
  if (!record || typeof record !== 'object') return ''
  return record.title != null ? String(record.title) : (record.name != null ? String(record.name) : '')
}

function bodyOf(record) {
  if (!record || typeof record !== 'object') return null
  if (record.card_body != null) return record.card_body
  return record.body != null ? record.body : null
}

// Historical/contractless task grouping for detail display. Parse ONLY the
// explicit phase tokens D1,D2,D3,D4,DEV1,DEV2,DEV3,DEV4,PC1 from the title,
// case-insensitively and token-bounded. Display grouping only — it never
// changes authority state.
function legacyPhaseOf(record) {
  if (!record || typeof record !== 'object') return null
  const title = titleOf(record)
  if (!title) return null
  const tokens = title.toUpperCase().match(/\b(?:DEV[1-4]|D[1-4]|PC1)\b/g)
  if (!tokens || tokens.length === 0) return null
  return normalizePhase(tokens[0])
}

function textOf(value) {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(textOf).join(', ')
  if (typeof value === 'object') {
    return Object.keys(value)
      .map((k) => `${k}: ${textOf(value[k])}`)
      .filter((part) => !part.endsWith(': '))
      .join('; ')
  }
  return String(value)
}

function Card({ record, kind, onOpenDetail }) {
  const id = kind === 'initiative' ? record.initiative_id : record.task_id
  const phase = phaseOf(record)
  const segment = segmentOf(record)
  const status = nativeStatusOf(record)
  const title = titleOf(record) || (kind === 'initiative' ? 'Untitled initiative' : 'Untitled task')
  const invalid = !phase
  const cardStyle = Object.assign({}, invalid ? styles.cardInvalid : styles.card)
  const cardProps = { style: cardStyle }
  if (kind === 'initiative' && typeof onOpenDetail === 'function') {
    cardProps.className = 'adrian-kanban-card-clickable'
    cardProps.role = 'button'
    cardProps.tabIndex = 0
    cardProps['aria-label'] = 'Open initiative ' + (id != null ? id : title)
    cardProps.onClick = () => onOpenDetail(id)
    cardProps.onKeyDown = (event) => {
      if (!event || !event.key) return
      if (event.key === 'Enter' || event.key === ' ') {
        if (typeof event.preventDefault === 'function') event.preventDefault()
        onOpenDetail(id)
      }
    }
  }
  return h('div', cardProps,
    h('div', { style: styles.cardTitle },
      title,
      id != null ? h('span', { style: { fontSize: 11, color: 'var(--color-muted-foreground, #616e7c)' } }, ' #' + id) : null),
    h('div', { style: styles.cardMeta },
      h('span', { style: styles.badgePhase }, phase ? phase : 'INVALID PHASE'),
      segment ? h('span', { style: styles.badge }, 'SEG ' + segment) : null,
      kind === 'task' && status ? h('span', { style: styles.taskStatus }, status) : null),
    invalid ? h('div', { style: { fontSize: 11, color: '#9b1c1c', marginTop: 6 } }, 'Missing or unrecognized lifecycle phase') : null
  )
}

function Diagnostics({ board }) {
  const failedChecks = Array.isArray(board.failed_checks) ? board.failed_checks : []
  const notEvaluated = Array.isArray(board.not_evaluated_checks) ? board.not_evaluated_checks : []
  return h('div', { style: styles.diagnostics },
    failedChecks.map((check, i) => {
      const c = check && typeof check === 'object' ? check : {}
      return h('div', { key: 'f-' + i, style: styles.diagnosticWarn },
        h('strong', null, 'failed_check'),
        c.code != null ? h('span', { style: styles.diagnosticField }, ' code: ' + textOf(c.code)) : '',
        c.target != null ? h('span', { style: styles.diagnosticField }, ' target: ' + textOf(c.target)) : '',
        c.expected != null ? h('span', { style: styles.diagnosticField }, ' expected: ' + textOf(c.expected)) : '',
        c.observed != null ? h('span', { style: styles.diagnosticField }, ' observed: ' + textOf(c.observed)) : '',
        c.accepted_format != null ? h('span', { style: styles.diagnosticField }, ' accepted_format: ' + textOf(c.accepted_format)) : '',
        c.remediation != null ? h('span', { style: styles.diagnosticField }, ' remediation: ' + textOf(c.remediation)) : '',
        c.responsible_actor != null ? h('span', { style: styles.diagnosticField }, ' responsible_actor: ' + textOf(c.responsible_actor)) : '',
        c.retry != null ? h('span', { style: styles.diagnosticField }, ' retry: ' + textOf(c.retry)) : ''
      )
    }),
    notEvaluated.map((check, i) =>
      h('div', { key: 'n-' + i, style: styles.diagnostic }, 'not_evaluated_check: ' + textOf(check))
    )
  )
}

function Column({ phase, initiatives, tasks, onOpenDetail }) {
  const total = initiatives.length + tasks.length
  return h('section', { style: styles.column, 'data-phase': phase },
    h('header', { style: styles.columnHeader },
      h('h2', { style: styles.columnTitle }, phase),
      h('span', { style: styles.count }, String(total))),
    h('div', null,
      total === 0 ? h('div', { style: styles.empty }, 'No cards') : null,
      initiatives.map((record, i) => h(Card, { key: 'i-' + i, record, kind: 'initiative', onOpenDetail })),
      tasks.map((record, i) => h(Card, { key: 't-' + i, record, kind: 'task' }))
    )
  )
}

function InitiativeDetail({ data, loading, error, onClose }) {
  let panel
  if (loading) {
    panel = h('div', { style: styles.detailPanel, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Initiative detail' },
      h('div', { style: styles.detailHeader },
        h('h2', { style: styles.detailTitle }, 'Initiative detail'),
        h('button', { style: styles.detailClose, onClick: onClose, type: 'button', 'aria-label': 'Close initiative detail' }, 'Close')),
      h('div', { style: Object.assign({}, styles.detailBody, styles.info) }, 'Loading initiative detail…'))
  } else if (error) {
    panel = h('div', { style: styles.detailPanel, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Initiative detail' },
      h('div', { style: styles.detailHeader },
        h('h2', { style: styles.detailTitle }, 'Initiative detail'),
        h('button', { style: styles.detailClose, onClick: onClose, type: 'button', 'aria-label': 'Close initiative detail' }, 'Close')),
      h('div', { style: styles.detailBody },
        h('div', { style: styles.diagnosticError }, 'Detail error: ' + error)))
  } else if (!data || typeof data !== 'object') {
    panel = h('div', { style: styles.detailPanel, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Initiative detail' },
      h('div', { style: styles.detailHeader },
        h('h2', { style: styles.detailTitle }, 'Initiative detail'),
        h('button', { style: styles.detailClose, onClick: onClose, type: 'button', 'aria-label': 'Close initiative detail' }, 'Close')),
      h('div', { style: styles.detailBody },
        h('div', { style: styles.diagnosticError }, 'Initiative detail is unavailable; close and reopen the initiative.')))
  } else {
    const card = data.card && typeof data.card === 'object' ? data.card : {}
    const bodyText = data.card_body != null ? data.card_body : (data.body != null ? data.body : '')
    const transition = data.current_transition && typeof data.current_transition === 'object' ? data.current_transition : null
    const history = Array.isArray(data.transition_history) ? data.transition_history
      : (Array.isArray(data.transitions) ? data.transitions : [])
    const phaseTasks = Array.isArray(data.tasks) ? data.tasks : []

    // Group associated historical/evidence tasks by display phase.
    const groups = {}
    phaseTasks.forEach((t) => {
      const gp = phaseOf(t) || legacyPhaseOf(t) || 'Legacy/Unclassified'
      if (!groups[gp]) groups[gp] = []
      groups[gp].push(t)
    })
    const groupNames = Object.keys(groups)

    panel = h('div', { style: styles.detailPanel, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Initiative detail' },
      h('div', { style: styles.detailHeader },
        h('h2', { style: styles.detailTitle }, titleOf(card) || 'Initiative detail'),
        h('button', { style: styles.detailClose, onClick: onClose, type: 'button', 'aria-label': 'Close initiative detail' }, 'Close')),
      h('div', { style: styles.detailBody },
        h('div', { style: styles.detailSection },
          h('h3', { style: styles.detailSectionTitle }, 'Description'),
          bodyText
            ? h('div', { style: styles.cardBody }, bodyText)
            : h('div', { style: styles.info }, 'No description stored')),
        h('div', { style: styles.detailSection },
          h('h3', { style: styles.detailSectionTitle }, 'Current phase'),
          h('div', { style: styles.cardMeta },
            h('span', { style: styles.badgePhase },
              transition && transition.to_phase != null ? String(transition.to_phase) : 'No transition recorded'),
            transition && transition.to_segment_id != null
              ? h('span', { style: styles.badge }, 'SEG ' + transition.to_segment_id)
              : null)),
        h('div', { style: styles.detailSection },
          h('h3', { style: styles.detailSectionTitle }, 'Transition history (' + history.length + ')'),
          history.length === 0
            ? h('div', { style: styles.info }, 'No transitions recorded')
            : history.map((t, i) => {
                const d = t && typeof t === 'object' ? t : {}
                return h('div', { key: 'h-' + i, style: styles.diagnostic },
                  'transition: ' + (d.from_phase != null ? d.from_phase : '?') + ' → ' + (d.to_phase != null ? d.to_phase : '?') +
                  (d.created_at != null ? ' @ ' + d.created_at : ''))
              })),
        phaseTasks.length === 0
          ? h('div', { style: styles.info }, 'No associated tasks recorded')
          : groupNames.map((gp) => {
              const items = groups[gp]
              return h('details', { key: 'g-' + gp, style: styles.detailGroup },
                h('summary', { style: styles.detailGroupSummary }, gp + ' tasks (' + items.length + ')'),
                items.map((t, i) => {
                  const d = t && typeof t === 'object' ? t : {}
                  const tid = d.task_id != null ? d.task_id : (d.id != null ? d.id : '(unknown)')
                  const attachments = Array.isArray(d.attachments) ? d.attachments : []
                  return h('div', { key: 't-' + i, style: styles.taskEntry },
                    h('div', { style: styles.diagnostic },
                      String(tid) + ' — ' + (d.title != null ? d.title : '(untitled)') +
                      (d.status != null ? ' [' + d.status + ']' : '')),
                    attachments.length > 0 ? h('div', { style: styles.attachments },
                      attachments.map((att, ai) => {
                        const ad = att && typeof att === 'object' ? att : {}
                        return h('div', { key: 'a-' + ai, style: styles.attachment },
                          (ad.filename != null ? ad.filename : '(unnamed)') +
                          (ad.content_type != null ? ' (' + ad.content_type + ')' : '') +
                          (ad.size != null ? ' [' + ad.size + ' bytes]' : ''))
                      })) : null
                  )
                }))
            })
      )
    )
  }
  return h('div', {
    style: styles.detailOverlay,
    role: 'presentation',
    onClick: (event) => {
      if (event && event.target === event.currentTarget) onClose()
    }
  }, panel)
}

function AdrianKanbanPage({ ctx }) {
  const [state, setState] = useState({
    loading: true,
    error: null,
    projects: [],
    handshake: null,
    boardEnvelope: null,
    mismatch: false,
    detailLoading: false,
    detailError: null,
    detailData: null
  })
  const timerRef = useRef(null)
  const [selectedBoard, setSelectedBoard] = useState('')
  const selectedBoardRef = useRef('')
  const [openInitiative, setOpenInitiative] = useState(null)
  const detailAbortRef = useRef(null)

  function load(boardOverride) {
    const board = typeof boardOverride === 'string' ? boardOverride : selectedBoardRef.current
    const boardParam = board ? `?board=${encodeURIComponent(board)}` : ''
    setState((prev) => ({ ...prev, loading: true, error: null, mismatch: false }))
    Promise.all([
      ctx.rest('/handshake'),
      ctx.rest('/projects'),
      ctx.rest(`/board${boardParam}`)
    ])
      .then(([handshake, projectsResponse, boardEnvelope]) => {
        const projects = projectsResponse && Array.isArray(projectsResponse.projects)
          ? projectsResponse.projects
          : []
        setState((prev) => ({
          ...prev,
          loading: false,
          error: null,
          handshake,
          boardEnvelope,
          projects,
          mismatch: false,
          detailLoading: prev.detailLoading,
          detailError: prev.detailError,
          detailData: prev.detailData
        }))
      })
      .catch((err) => {
        setState((prev) => ({
          ...prev,
          loading: false,
          error: err && err.message ? err.message : 'Failed to load Kanban board data',
          handshake: null,
          boardEnvelope: null,
          mismatch: false,
          detailLoading: prev.detailLoading,
          detailError: prev.detailError,
          detailData: prev.detailData
        }))
      })
  }

  useEffect(() => {
    load()
    timerRef.current = setInterval(load, POLL_MS)
    return () => {
      clearInterval(timerRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function handleBoardChange(event) {
    const value = event.target.value
    setSelectedBoard(value)
    selectedBoardRef.current = value
    closeInitiativeDetail()
    load(value)
  }

  function openInitiativeDetail(id) {
    if (id == null) return
    if (typeof detailAbortRef.current !== 'undefined' && detailAbortRef.current &&
        typeof detailAbortRef.current.abort === 'function') {
      detailAbortRef.current.abort()
    }
    let controller = null
    if (typeof AbortController === 'function') {
      controller = new AbortController()
      detailAbortRef.current = controller
    }
    setOpenInitiative(id)
    setState((prev) => ({ ...prev, detailLoading: true, detailError: null, detailData: null }))
    ctx.rest('/initiatives/' + encodeURIComponent(id), controller ? { signal: controller.signal } : undefined)
      .then((envelope) => {
        if (envelope && typeof envelope === 'object' && envelope.result === 'ACCEPTED' &&
            envelope.value != null && typeof envelope.value === 'object') {
          setState((prev) => ({ ...prev, detailLoading: false, detailError: null, detailData: envelope.value }))
        } else {
          setState((prev) => ({ ...prev, detailLoading: false, detailError: 'Initiative detail response was REJECTED or malformed', detailData: null }))
        }
      })
      .catch((err) => {
        if (err && err.name === 'AbortError') return
        setState((prev) => ({ ...prev, detailLoading: false, detailError: err && err.message ? err.message : 'Failed to load initiative detail', detailData: null }))
      })
  }

  function closeInitiativeDetail() {
    if (typeof detailAbortRef.current !== 'undefined' && detailAbortRef.current &&
        typeof detailAbortRef.current.abort === 'function') {
      detailAbortRef.current.abort()
      detailAbortRef.current = null
    }
    setOpenInitiative(null)
    setState((prev) => ({ ...prev, detailLoading: false, detailError: null, detailData: null }))
  }

  if (state.loading) {
    return h('div', { style: styles.page },
      h('div', { style: styles.info }, 'Loading Initiative Tracker…')
    )
  }

  if (state.error) {
    return h('div', { style: styles.page },
      h('div', { style: styles.error }, 'Fetch failed: ' + state.error),
      h('button', { style: styles.button, onClick: () => load(), type: 'button' }, 'Refresh')
    )
  }

  const handshake = state.handshake || {}
  const versions = handshake.versions || {}
  const boardAccepted = !!(state.boardEnvelope && state.boardEnvelope.result === 'ACCEPTED')
  const board = boardAccepted && state.boardEnvelope.value ? state.boardEnvelope.value : {}
  // Derived strictly from the accepted board envelope, after validation.
  const initiatives = Array.isArray(board.initiatives) ? board.initiatives : []
  const tasks = Array.isArray(board.tasks) ? board.tasks : []

  const controlsEnabled =
    handshake.authority === 'adrian-kanban' &&
    String(versions.protocol_version) === EXPECTED_PROTOCOL_VERSION &&
    handshake.mutation_controls_enabled === true

  const initiativeById = {}
  initiatives.forEach((record) => {
    if (record && record.initiative_id != null) initiativeById[record.initiative_id] = record
  })

  // Board positioning: drop closed tasks FIRST; an open task is shown only
  // when its normalized display phase equals its parent initiative's current
  // phase. Closed historical tasks do not appear on the board and do not
  // emit diagnostics; they remain reachable through the initiative detail.
  const byPhase = {}
  const unpositioned = []
  PHASES.forEach((p) => {
    byPhase[p] = { initiatives: [], tasks: [] }
  })
  initiatives.forEach((record) => {
    const p = phaseOf(record)
    if (p) byPhase[p].initiatives.push(record)
    else unpositioned.push({ kind: 'initiative', record })
  })
  tasks.forEach((record) => {
    if (!isTaskOpen(record)) return
    const p = phaseOf(record)
    if (!p) {
      unpositioned.push({ kind: 'task', record })
      return
    }
    const parent = record.initiative_id != null ? initiativeById[record.initiative_id] : null
    const parentPhase = parent ? phaseOf(parent) : null
    if (parentPhase == null) {
      // Open task without a recognizable parent initiative: one diagnostic.
      if (!unpositioned.some((u) => u.kind === 'task' && u.record && u.record.task_id === record.task_id)) {
        unpositioned.push({ kind: 'task', record })
      }
      return
    }
    if (parentPhase !== p) return
    byPhase[p].tasks.push(record)
  })

  return h('div', { style: styles.page },
    h('header', { style: styles.toolbar },
      h('h1', { style: styles.title }, 'Initiative Tracker'),
      h('div', { style: styles.actions },
        h('select', {
          style: styles.button,
          value: selectedBoard,
          onChange: handleBoardChange,
          'aria-label': 'Project'
        },
        h('option', { value: '' }, 'All projects'),
        state.projects.map((project) => h('option', {
          key: project.board,
          value: project.board
        }, project.name))),
        h('button', { style: styles.button, onClick: () => load(), type: 'button' }, 'Refresh'))),
    !controlsEnabled ? h('div', { style: styles.warn },
      'Mutation controls are disabled: handshake authority, protocol version, or mutation_controls_enabled does not match.') : null,
    !boardAccepted ? h('div', { style: styles.error },
      'Board boundary REJECTED or malformed: no board data is available') : null,
    boardAccepted ? h(Diagnostics, { board }) : null,
    unpositioned.length ? h('div', { style: styles.diagnostics },
      unpositioned.map((item, i) => {
        const id = item.kind === 'initiative' ? item.record.initiative_id : item.record.task_id
        return h('div', { key: 'u-' + i, style: styles.unpositioned },
          (item.kind === 'initiative' ? 'initiative_id' : 'task_id') + ': ' + (id != null ? id : '(unknown)') +
          ' — Missing or unrecognized lifecycle phase')
      })
    ) : null,
    h('div', { style: styles.columns },
      PHASES.map((p) => h(Column, {
        key: p,
        phase: p,
        initiatives: byPhase[p].initiatives,
        tasks: byPhase[p].tasks,
        onOpenDetail: openInitiativeDetail
      }))
    ),
    openInitiative != null ? h(InitiativeDetail, {
      data: state.detailData,
      loading: state.detailLoading,
      error: state.detailError,
      onClose: closeInitiativeDetail
    }) : null
  )
}

const plugin = {
  id: 'adrian-kanban',
  name: 'Initiative Tracker',
  description: 'Initiative Tracker lifecycle board — nine-phase projection of initiatives and subordinate tasks with actionable boundary diagnostics.',
  defaultEnabled: false,
  register(ctx) {
    const Page = () => h(AdrianKanbanPage, { ctx })

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: '/kanban' },
        render: Page
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 50,
        data: { codicon: 'project', label: 'Initiative Tracker', path: '/kanban' }
      }
    ])
  }
}

export default plugin
