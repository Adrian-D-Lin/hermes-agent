import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import * as React from 'react'

const { useEffect, useRef, useState } = React

function h(type, props) {
  const children = Array.prototype.slice.call(arguments, 2)
  return React.createElement(type, props, children.length ? children : undefined)
}

const EXPECTED_PROTOCOL_VERSION = '2'
const PHASES = Object.freeze(['D1', 'D2', 'D3', 'D4', 'DEV1', 'DEV2', 'DEV3', 'DEV4', 'PC1']);
const POLL_MS = 30000

const styles = {
  page: { display: 'flex', flexDirection: 'column', gap: 12, padding: 16 },
  toolbar: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  title: { margin: 0, fontSize: 18 },
  button: { cursor: 'pointer', padding: '4px 10px' },
  warn: { border: '1px solid #b45309', background: '#fef3c7', color: '#92400e', padding: 8, borderRadius: 4 },
  error: { border: '1px solid #b91c1c', background: '#fee2e2', color: '#7f1d1d', padding: 8, borderRadius: 4 },
  info: { border: '1px solid #2563eb', background: '#dbeafe', color: '#1e3a8a', padding: 8, borderRadius: 4 },
  columns: { display: 'flex', gap: 12, overflowX: 'auto', alignItems: 'flex-start' },
  column: { minWidth: 200, flex: '0 0 auto', border: '1px solid #d1d5db', borderRadius: 4, padding: 8 },
  columnHeader: { display: 'flex', justifyContent: 'space-between', marginBottom: 8 },
  columnTitle: { margin: 0, fontSize: 14 },
  count: { fontSize: 12, color: '#6b7280' },
  empty: { fontSize: 12, color: '#6b7280' },
  card: { border: '1px solid #d1d5db', borderRadius: 4, padding: 6, marginBottom: 6, background: '#fff' },
  cardInvalid: { border: '1px solid #b91c1c', background: '#fef2f2' },
  cardTitle: { fontWeight: 600, fontSize: 13 },
  cardMeta: { display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 },
  badge: { fontSize: 11, padding: '1px 5px', borderRadius: 3, background: '#e5e7eb', color: '#374151' },
  taskStatus: { fontSize: 11, color: '#0369a1' },
  diagnostics: { display: 'flex', flexDirection: 'column', gap: 6 },
  diagnostic: { fontSize: 12, padding: 6, borderRadius: 4, background: '#f3f4f6' },
  unpositioned: { fontSize: 12, color: '#7f1d1d' }
}

function phaseOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.current_phase != null ? record.current_phase : record.lifecycle_phase
  if (raw == null) return null
  const p = String(raw).toUpperCase()
  return PHASES.indexOf(p) >= 0 ? p : null
}

function segmentOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.current_segment_id != null ? record.current_segment_id : record.segment_id
  return raw == null ? null : String(raw)
}

function statusOf(record) {
  if (!record || typeof record !== 'object') return null
  const raw = record.status != null ? record.status : record.native_status
  return raw == null ? null : String(raw)
}

function titleOf(record) {
  if (!record || typeof record !== 'object') return ''
  return record.title != null ? String(record.title) : (record.name != null ? String(record.name) : '')
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

function Card({ record, kind }) {
  const id = kind === 'initiative' ? record.initiative_id : record.task_id
  const phase = phaseOf(record)
  const segment = segmentOf(record)
  const status = statusOf(record)
  const title = titleOf(record) || (kind === 'initiative' ? 'Untitled initiative' : 'Untitled task')
  const invalid = !phase
  return h('div', { style: invalid ? styles.cardInvalid : styles.card },
    h('div', { style: styles.cardTitle },
      title,
      id != null ? h('span', null, ' #' + id) : null),
    h('div', { style: styles.cardMeta },
      h('span', { style: styles.badge }, phase ? phase : 'INVALID PHASE'),
      segment ? h('span', { style: styles.badge }, 'SEG ' + segment) : null,
      kind === 'task' && status ? h('span', { style: styles.taskStatus }, status) : null),
    invalid ? h('div', { style: { fontSize: 11, color: '#7f1d1d' } }, 'Missing or unrecognized lifecycle phase') : null
  )
}

function Diagnostics({ board }) {
  const failedChecks = Array.isArray(board.failed_checks) ? board.failed_checks : []
  const notEvaluated = Array.isArray(board.not_evaluated_checks) ? board.not_evaluated_checks : []
  return h('div', { style: styles.diagnostics },
    failedChecks.map((check, i) => {
      const c = check && typeof check === 'object' ? check : {}
      return h('div', { key: 'f-' + i, style: styles.diagnostic },
        h('strong', null, 'failed_check'),
        c.code != null ? ' code: ' + textOf(c.code) : '',
        c.target != null ? ' target: ' + textOf(c.target) : '',
        c.expected != null ? ' expected: ' + textOf(c.expected) : '',
        c.observed != null ? ' observed: ' + textOf(c.observed) : '',
        c.accepted_format != null ? ' accepted_format: ' + textOf(c.accepted_format) : '',
        c.remediation != null ? ' remediation: ' + textOf(c.remediation) : '',
        c.responsible_actor != null ? ' responsible_actor: ' + textOf(c.responsible_actor) : '',
        c.retry != null ? ' retry: ' + textOf(c.retry) : ''
      )
    }),
    notEvaluated.map((check, i) =>
      h('div', { key: 'n-' + i, style: styles.diagnostic }, 'not_evaluated_check: ' + textOf(check))
    )
  )
}

function Column({ phase, initiatives, tasks }) {
  const total = initiatives.length + tasks.length
  return h('section', { style: styles.column, 'data-phase': phase },
    h('header', { style: styles.columnHeader },
      h('h2', { style: styles.columnTitle }, phase),
      h('span', { style: styles.count }, String(total))),
    h('div', null,
      total === 0 ? h('div', { style: styles.empty }, 'No cards') : null,
      initiatives.map((record, i) => h(Card, { key: 'i-' + i, record, kind: 'initiative' })),
      tasks.map((record, i) => h(Card, { key: 't-' + i, record, kind: 'task' }))
    )
  )
}

function AdrianKanbanPage({ ctx }) {
  const [state, setState] = useState({
    loading: true,
    error: null,
    handshake: null,
    boardEnvelope: null,
    mismatch: false
  })
  const timerRef = useRef(null)

  function load() {
    setState((prev) => ({ ...prev, loading: true, error: null, mismatch: false }))
    Promise.all([ctx.rest('/handshake'), ctx.rest('/board')])
      .then(([handshake, boardEnvelope]) => {
        setState({
          loading: false,
          error: null,
          handshake,
          boardEnvelope,
          mismatch: false
        })
      })
      .catch((err) => {
        setState({
          loading: false,
          error: err && err.message ? err.message : 'Failed to load Kanban board data',
          handshake: null,
          boardEnvelope: null,
          mismatch: false
        })
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

  if (state.loading) {
    return h('div', { style: styles.page },
      h('div', { style: styles.info }, 'Loading Kanban board…')
    )
  }

  if (state.error) {
    return h('div', { style: styles.page },
      h('div', { style: styles.error }, 'Fetch failed: ' + state.error),
      h('button', { style: styles.button, onClick: load, type: 'button' }, 'Refresh')
    )
  }

  const handshake = state.handshake || {}
  const versions = handshake.versions || {}
  const boardAccepted = !!(state.boardEnvelope && state.boardEnvelope.result === 'ACCEPTED')
  const board = boardAccepted && state.boardEnvelope.value ? state.boardEnvelope.value : {}
  const initiatives = Array.isArray(board.initiatives) ? board.initiatives : []
  const tasks = Array.isArray(board.tasks) ? board.tasks : []

  const controlsEnabled =
    handshake.authority === 'adrian-kanban' &&
    String(versions.protocol_version) === EXPECTED_PROTOCOL_VERSION &&
    handshake.mutation_controls_enabled === true

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
    const p = phaseOf(record)
    if (p) byPhase[p].tasks.push(record)
    else unpositioned.push({ kind: 'task', record })
  })

  return h('div', { style: styles.page },
    h('header', { style: styles.toolbar },
      h('h1', { style: styles.title }, 'Adrian Kanban'),
      h('button', { style: styles.button, onClick: load, type: 'button' }, 'Refresh')),
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
        tasks: byPhase[p].tasks
      }))
    )
  )
}

const plugin = {
  id: 'adrian-kanban',
  name: 'Kanban',
  description: 'Adrian Kanban lifecycle board — nine-phase projection of initiatives and subordinate tasks with actionable boundary diagnostics.',
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
        data: { codicon: 'project', label: 'Kanban', path: '/kanban' }
      }
    ])
  }
}

export default plugin
