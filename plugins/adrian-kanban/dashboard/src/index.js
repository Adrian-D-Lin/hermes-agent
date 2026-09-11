(function () {
  'use strict';

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const API_ROOT = '/api/plugins/adrian-kanban';
  const PHASES = Object.freeze(["D1", "D2", "D3", "D4", "DEV1", "DEV2", "DEV3", "DEV4", "PC1"]);
  const EXPECTED_PROTOCOL_VERSION = "2";
  const CLOSED_TASK_STATUSES = Object.freeze(['done', 'completed', 'archived', 'cancelled']);

  function api(path, options) {
    return SDK.fetchJSON(API_ROOT + path, options);
  }

  function h() {
    var args = Array.prototype.slice.call(arguments);
    var tag = args.shift();
    var props = args.length && args[0] != null && typeof args[0] === 'object' && !Array.isArray(args[0]) ? args.shift() : null;
    return React.createElement(tag, props, args.length ? args : undefined)
  }

  // Normalize an authoritative exact step (e.g. "DEV3.1", "DEV1.1a") to its
  // recognized top-level phase for DISPLAY only. Unknown values map to null.
  function normalizePhase(raw) {
    if (raw == null) return null;
    var p = String(raw).toUpperCase();
    var match = p.match(/^(D[1-4]|DEV[1-4]|PC1)(\.[0-9A-Z]+)?$/);
    if (match) return match[1];
    return null;
  }

  function phaseOf(record) {
    if (!record || typeof record !== 'object') return null;
    var raw = record.current_phase != null ? record.current_phase : record.lifecycle_phase;
    if (raw == null) return null;
    var p = String(raw).toUpperCase();
    if (PHASES.indexOf(p) >= 0) return p;
    return normalizePhase(p);
  }

  function segmentOf(record) {
    if (!record || typeof record !== 'object') return null;
    var raw = record.current_segment_id != null ? record.current_segment_id : record.segment_id;
    return raw == null ? null : String(raw);
  }

  function nativeStatusOf(record) {
    if (!record || typeof record !== 'object') return null;
    var raw = record.status != null ? record.status : record.native_status;
    return raw == null ? null : String(raw);
  }

  function isTaskOpen(record) {
    if (!record || typeof record !== 'object') return false;
    var status = nativeStatusOf(record);
    if (status == null) return true;
    var s = status.toLowerCase();
    for (var i = 0; i < CLOSED_TASK_STATUSES.length; i++) {
      if (s === CLOSED_TASK_STATUSES[i]) return false;
    }
    return true;
  }

  function titleOf(record) {
    if (!record || typeof record !== 'object') return '';
    return record.title != null ? String(record.title) : (record.name != null ? String(record.name) : '');
  }

  function bodyOf(record) {
    if (!record || typeof record !== 'object') return null;
    if (record.card_body != null) return record.card_body;
    return record.body != null ? record.body : null;
  }

  // Historical/contractless task grouping for detail display. Parse ONLY the
  // explicit phase tokens D1,D2,D3,D4,DEV1,DEV2,DEV3,DEV4,PC1 from the title.
  // This is display grouping only — it never changes authority state.
  function legacyPhaseOf(record) {
    if (!record || typeof record !== 'object') return null;
    var title = titleOf(record);
    if (!title) return null;
    var tokens = title.toUpperCase().match(/\b(?:DEV[1-4]|D[1-4]|PC1)\b/g);
    if (!tokens || tokens.length === 0) return null;
    var normalized = normalizePhase(tokens[0]);
    return normalized || null;
  }

  function Card(props) {
    var record = props.record;
    var kind = props.kind;
    var onOpenDetail = props.onOpenDetail;
    var phase = phaseOf(record);
    var segment = segmentOf(record);
    var status = nativeStatusOf(record);
    var id = kind === 'initiative' ? record.initiative_id : record.task_id;
    var title = titleOf(record) || (kind === 'initiative' ? 'Untitled initiative' : 'Untitled task');
    var invalid = !phase;
    var cardProps = { className: 'adrian-kanban-card' + (invalid ? ' adrian-kanban-card-invalid' : '') };
    if (kind === 'initiative' && typeof onOpenDetail === 'function') {
      cardProps.className += ' adrian-kanban-card-clickable';
      cardProps.role = 'button';
      cardProps.tabIndex = 0;
      cardProps['aria-label'] = 'Open initiative ' + (id != null ? id : title);
      cardProps.onClick = function () { onOpenDetail(id, record.board); };
      cardProps.onKeyDown = function (event) {
        if (!event || !event.key) return;
        if (event.key === 'Enter' || event.key === ' ') {
          if (typeof event.preventDefault === 'function') event.preventDefault();
          onOpenDetail(id, record.board);
        }
      };
    }
    return h('div', cardProps,
      h('div', { className: 'adrian-kanban-card-title' }, title,
        id != null ? h('span', { className: 'adrian-kanban-card-id' }, ' #' + id) : null),
      h('div', { className: 'adrian-kanban-card-meta' },
        h('span', { className: 'adrian-kanban-badge adrian-kanban-badge-phase' }, phase ? phase : 'INVALID PHASE'),
        segment ? h('span', { className: 'adrian-kanban-badge adrian-kanban-badge-segment' }, 'SEG ' + segment) : null,
        kind === 'task' && status ? h('span', { className: 'adrian-kanban-task-status' }, status) : null
      ),
      invalid ? h('div', { className: 'adrian-kanban-card-error' }, 'Missing or unrecognized lifecycle phase') : null
    );
  }

  function textOf(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(textOf).join(', ');
    if (typeof value === 'object') {
      var parts = [];
      Object.keys(value).forEach(function (k) {
        var v = value[k];
        if (v == null) return;
        parts.push(k + ': ' + textOf(v));
      });
      return parts.join('; ');
    }
    return String(value);
  }

  function StructuredSection(props) {
    var rendered = textOf(props.value);
    return h('details', { className: 'adrian-kanban-detail-group' },
      h('summary', null, props.label),
      h('pre', { className: 'adrian-kanban-structured-value' }, rendered || 'None recorded')
    );
  }

  function Column(props) {
    var phase = props.phase;
    var initiatives = props.initiatives;
    var tasks = props.tasks;
    return h('section', { className: 'adrian-kanban-column', 'data-phase': phase },
      h('header', { className: 'adrian-kanban-column-header' },
        h('h2', null, phase),
        h('span', { className: 'adrian-kanban-column-count' }, String(initiatives.length + tasks.length))
      ),
      h('div', { className: 'adrian-kanban-column-body' },
        initiatives.length + tasks.length === 0
          ? h('div', { className: 'adrian-kanban-column-empty' }, 'No cards')
          : null,
        initiatives.map(function (r, i) {
          return h(Card, { key: 'i-' + i, record: r, kind: 'initiative', onOpenDetail: props.onOpenDetail });
        }),
        tasks.map(function (r, i) {
          return h(Card, { key: 't-' + i, record: r, kind: 'task' });
        })
      )
    );
  }

  function checkField(label, value) {
    if (value == null) return null;
    return h('span', { className: 'adrian-kanban-diagnostic-field' }, label + ': ' + textOf(value));
  }

  function Diagnostics(props) {
    var board = props.board;
    var failedChecks = Array.isArray(board.failed_checks) ? board.failed_checks : [];
    var notEvaluated = Array.isArray(board.not_evaluated_checks) ? board.not_evaluated_checks : [];
    return h('div', { className: 'adrian-kanban-diagnostics' },
      failedChecks.map(function (check, i) {
        var c = check && typeof check === 'object' ? check : {};
        return h('div', { key: 'f-' + i, className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-warn' },
          h('span', { className: 'adrian-kanban-diagnostic-label' }, 'failed_check'),
          checkField('code', c.code),
          checkField('target', c.target),
          checkField('observed', c.observed),
          checkField('expected', c.expected),
          checkField('accepted_format', c.accepted_format),
          checkField('remediation', c.remediation)
        );
      }),
      notEvaluated.map(function (check, i) {
        return h('div', { key: 'n-' + i, className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' },
          'not_evaluated_check: ' + textOf(check));
      })
    );
  }

  function InitiativeDetail(props) {
    var data = props.data;
    var loading = props.loading;
    var error = props.error;
    var onClose = props.onClose;

    var panel = h('div', {
      className: 'adrian-kanban-detail-panel',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': 'Initiative detail'
    });

    if (loading) {
      panel = h('div', { className: 'adrian-kanban-detail-panel', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'adrian-kanban-detail-header' },
          h('h2', null, 'Initiative detail'),
          h('button', { className: 'adrian-kanban-detail-close', onClick: onClose, type: 'button' }, 'Close')),
        h('div', { className: 'adrian-kanban-detail-body adrian-kanban-diagnostic adrian-kanban-diagnostic-info' },
          'Loading initiative detail…'));
    } else if (error) {
      panel = h('div', { className: 'adrian-kanban-detail-panel', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'adrian-kanban-detail-header' },
          h('h2', null, 'Initiative detail'),
          h('button', { className: 'adrian-kanban-detail-close', onClick: onClose, type: 'button' }, 'Close')),
        h('div', { className: 'adrian-kanban-detail-body' },
          h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-error' },
            'Detail error: ' + error)));
    } else if (!data || typeof data !== 'object') {
      panel = h('div', { className: 'adrian-kanban-detail-panel', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'adrian-kanban-detail-header' },
          h('h2', null, 'Initiative detail'),
          h('button', { className: 'adrian-kanban-detail-close', onClick: onClose, type: 'button' }, 'Close')),
        h('div', { className: 'adrian-kanban-detail-body' },
          h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-error' },
            'Initiative detail is unavailable; close and reopen the initiative.')));
    } else {
      var card = data.card && typeof data.card === 'object' ? data.card : {};
      var bodyText = data.card_body != null ? data.card_body : (data.body != null ? data.body : '');
      var transition = data.current_transition && typeof data.current_transition === 'object' ? data.current_transition : null;
      var history = Array.isArray(data.transition_history) ? data.transition_history
        : (Array.isArray(data.transitions) ? data.transitions : []);
      var phaseTasks = Array.isArray(data.tasks) ? data.tasks : [];
      var phaseResults = Array.isArray(data.phase_results) ? data.phase_results : [];
      var segmentProjection = data.segment_projection && typeof data.segment_projection === 'object'
        ? data.segment_projection : null;
      var workspaces = Array.isArray(data.workspaces) ? data.workspaces : [];
      var openFindings = Array.isArray(data.open_findings) ? data.open_findings : [];
      var nextPermittedRoutes = Array.isArray(data.next_permitted_routes) ? data.next_permitted_routes : [];

      // Group associated historical/evidence tasks by display phase.
      var groups = {};
      phaseTasks.forEach(function (t) {
        var gp = phaseOf(t) || legacyPhaseOf(t) || 'Legacy/Unclassified';
        if (!groups[gp]) groups[gp] = [];
        groups[gp].push(t);
      });
      var groupNames = Object.keys(groups);

      panel = h('div', { className: 'adrian-kanban-detail-panel', role: 'dialog', 'aria-modal': 'true' },
        h('div', { className: 'adrian-kanban-detail-header' },
          h('h2', null, titleOf(card) || 'Initiative detail'),
          h('button', { className: 'adrian-kanban-detail-close', onClick: onClose, type: 'button', 'aria-label': 'Close initiative detail' }, 'Close')),
        h('div', { className: 'adrian-kanban-detail-body' },
          h('div', { className: 'adrian-kanban-detail-section' },
            h('h3', null, 'Description'),
            bodyText ? h('div', { className: 'adrian-kanban-card-body' }, bodyText)
              : h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' }, 'No description stored')),
          h('div', { className: 'adrian-kanban-detail-section' },
            h('h3', null, 'Current phase'),
            h('div', { className: 'adrian-kanban-card-meta' },
              h('span', { className: 'adrian-kanban-badge adrian-kanban-badge-phase' },
                transition ? (transition.to_phase != null ? String(transition.to_phase) : 'No transition recorded') : 'No transition recorded'),
              transition && transition.to_segment_id != null
                ? h('span', { className: 'adrian-kanban-badge adrian-kanban-badge-segment' }, 'SEG ' + transition.to_segment_id)
                : null)),
          h('div', { className: 'adrian-kanban-detail-section' },
            h('h3', null, 'Transition history (' + history.length + ')'),
            history.length === 0
              ? h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' }, 'No transitions recorded')
              : history.map(function (t, i) {
                  var d = t && typeof t === 'object' ? t : {};
                  return h('div', { key: 'h-' + i, className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' },
                    'transition: ' + (d.from_phase != null ? d.from_phase : '?') + ' → ' + (d.to_phase != null ? d.to_phase : '?') +
                    (d.created_at != null ? ' @ ' + d.created_at : ''));
                })),
          h(StructuredSection, { label: 'Phase results (' + phaseResults.length + ')', value: phaseResults }),
          h(StructuredSection, { label: 'Segment manifest and readiness', value: segmentProjection }),
          h(StructuredSection, { label: 'Workspace assignments (' + workspaces.length + ')', value: workspaces }),
          h(StructuredSection, { label: 'Open findings (' + openFindings.length + ')', value: openFindings }),
          h(StructuredSection, { label: 'Next permitted routes (' + nextPermittedRoutes.length + ')', value: nextPermittedRoutes }),
          phaseTasks.length === 0
            ? h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' }, 'No associated tasks recorded')
            : groupNames.map(function (gp) {
                var items = groups[gp];
                return h('details', { key: 'g-' + gp, className: 'adrian-kanban-detail-group' },
                  h('summary', null, gp + ' tasks (' + items.length + ')'),
                  items.map(function (t, i) {
                    var d = t && typeof t === 'object' ? t : {};
                    var tid = d.task_id != null ? d.task_id : (d.id != null ? d.id : '(unknown)');
                    var attachments = Array.isArray(d.attachments) ? d.attachments : [];
                    return h('div', { key: 't-' + i, className: 'adrian-kanban-task-entry' },
                      h('div', { className: 'adrian-kanban-detail-record' },
                        String(tid) + ' — ' + (d.title != null ? d.title : '(untitled)') +
                        (d.status != null ? ' [' + d.status + ']' : '')),
                      d.lifecycle_contract != null
                        ? h('div', { className: 'adrian-kanban-detail-record' }, 'Lifecycle contract: ' + textOf(d.lifecycle_contract)) : null,
                      d.accepted_handoff != null
                        ? h('div', { className: 'adrian-kanban-detail-record' }, 'Accepted handoff: ' + textOf(d.accepted_handoff)) : null,
                      d.latest_candidate != null
                        ? h('div', { className: 'adrian-kanban-detail-record' }, 'Latest candidate: ' + textOf(d.latest_candidate)) : null,
                      attachments.length > 0 ? h('div', { className: 'adrian-kanban-attachments' },
                        attachments.map(function (att, ai) {
                          var ad = att && typeof att === 'object' ? att : {};
                          return h('div', { key: 'a-' + ai, className: 'adrian-kanban-attachment' },
                            (ad.filename != null ? ad.filename : '(unnamed)') +
                            (ad.content_type != null ? ' (' + ad.content_type + ')' : '') +
                            (ad.size != null ? ' [' + ad.size + ' bytes]' : ''));
                        })) : null
                    );
                  }));
              })
        )
      );
    }

    return h('div', {
      className: 'adrian-kanban-detail-overlay',
      role: 'presentation',
      onClick: function (event) {
        if (event && event.target && event.target.classList && event.target.classList.contains('adrian-kanban-detail-overlay')) {
          onClose();
        }
      }
    }, panel);
  }

  function App() {
    var state = React.useState({
      loading: true,
      error: null,
      handshake: null,
      board: null,
      boardEnvelope: null,
      mismatch: false,
      projects: [],
      detailLoading: false,
      detailError: null,
      detailData: null
    });
    var s = state[0];
    var set = state[1];
    var selectedBoardState = React.useState('');
    var selectedBoard = selectedBoardState[0];
    var setSelectedBoard = selectedBoardState[1];
    var selectedBoardRef = React.useRef('');
    var openInitiativeState = React.useState(null);
    var openInitiative = openInitiativeState[0];
    var setOpenInitiative = openInitiativeState[1];
    var detailAbortRef = React.useRef(null);

    function load(boardOverride) {
      set(function (prev) {
        return {
          loading: prev.boardEnvelope == null,
          error: null,
          handshake: prev.handshake,
          board: prev.board,
          boardEnvelope: prev.boardEnvelope,
          mismatch: false,
          projects: prev.projects,
          detailLoading: prev.detailLoading,
          detailError: prev.detailError,
          detailData: prev.detailData
        };
      });
      var requestedBoard = typeof boardOverride === 'string' ? boardOverride : selectedBoardRef.current;
      var boardPath = requestedBoard ? '/board?board=' + encodeURIComponent(requestedBoard) : '/board';
      return Promise.all([
        api('/handshake'),
        api('/projects'),
        api(boardPath)
      ]).then(function (results) {
        var handshake = results[0];
        var projectsResponse = results[1];
        var boardEnvelope = results[2];
        var projects = projectsResponse && Array.isArray(projectsResponse.projects) ? projectsResponse.projects : [];
        var board = null;
        if (boardEnvelope && typeof boardEnvelope === 'object' && boardEnvelope.result === 'ACCEPTED' &&
            boardEnvelope.value != null && typeof boardEnvelope.value === 'object') {
          board = boardEnvelope.value;
        }
        set(function (prev) {
          return {
            loading: false,
            error: null,
            handshake: handshake,
            board: board,
            boardEnvelope: boardEnvelope,
            mismatch: false,
            projects: projects,
            detailLoading: prev.detailLoading,
            detailError: prev.detailError,
            detailData: prev.detailData
          };
        });
      }).catch(function (err) {
        set(function (prev) {
          return {
            loading: false,
            error: err && err.message ? err.message : 'Failed to load dashboard data',
            handshake: null,
            board: null,
            boardEnvelope: null,
            mismatch: false,
            projects: prev.projects,
            detailLoading: false,
            detailError: prev.detailError,
            detailData: prev.detailData
          };
        });
      });
    }

    function openInitiativeDetail(id, board) {
      if (id == null || board == null || String(board).trim() === '') return;
      if (typeof detailAbortRef.current !== 'undefined' && detailAbortRef.current &&
          typeof detailAbortRef.current.abort === 'function') {
        detailAbortRef.current.abort();
      }
      var controller = null;
      if (typeof window.AbortController === 'function') {
        controller = new window.AbortController();
        detailAbortRef.current = controller;
      }
      setOpenInitiative(id);
      set(function (prev) {
        return {
          loading: prev.loading,
          error: prev.error,
          handshake: prev.handshake,
          board: prev.board,
          boardEnvelope: prev.boardEnvelope,
          mismatch: prev.mismatch,
          projects: prev.projects,
          detailLoading: true,
          detailError: null,
          detailData: null
        };
      });
      return api('/initiatives/' + encodeURIComponent(id) + '?board=' + encodeURIComponent(board), controller ? { signal: controller.signal } : undefined)
        .then(function (envelope) {
          if (envelope && typeof envelope === 'object' && envelope.result === 'ACCEPTED' &&
              envelope.value != null && typeof envelope.value === 'object') {
            set(function (prev) {
              return {
                loading: prev.loading,
                error: prev.error,
                handshake: prev.handshake,
                board: prev.board,
                boardEnvelope: prev.boardEnvelope,
                mismatch: prev.mismatch,
                projects: prev.projects,
                detailLoading: false,
                detailError: null,
                detailData: envelope.value
              };
            });
          } else {
            set(function (prev) {
              return {
                loading: prev.loading,
                error: prev.error,
                handshake: prev.handshake,
                board: prev.board,
                boardEnvelope: prev.boardEnvelope,
                mismatch: prev.mismatch,
                projects: prev.projects,
                detailLoading: false,
                detailError: 'Initiative detail response was REJECTED or malformed',
                detailData: null
              };
            });
          }
        })
        .catch(function (err) {
          if (err && err.name === 'AbortError') return;
          set(function (prev) {
            return {
              loading: prev.loading,
              error: prev.error,
              handshake: prev.handshake,
              board: prev.board,
              boardEnvelope: prev.boardEnvelope,
              mismatch: prev.mismatch,
              projects: prev.projects,
              detailLoading: false,
              detailError: (err && err.message ? err.message : 'Failed to load initiative detail'),
              detailData: null
            };
          });
        });
    }

    function closeInitiativeDetail() {
      if (typeof detailAbortRef.current !== 'undefined' && detailAbortRef.current &&
          typeof detailAbortRef.current.abort === 'function') {
        detailAbortRef.current.abort();
        detailAbortRef.current = null;
      }
      setOpenInitiative(null);
      set(function (prev) {
        return {
          loading: prev.loading,
          error: prev.error,
          handshake: prev.handshake,
          board: prev.board,
          boardEnvelope: prev.boardEnvelope,
          mismatch: prev.mismatch,
          projects: prev.projects,
          detailLoading: false,
          detailError: null,
          detailData: null
        };
      });
    }

    var wsRef = React.useRef(null);
    var cursorRef = React.useRef(0);
    var reconnectDelayRef = React.useRef(1000);
    var reconnectTimerRef = React.useRef(null);
    var debounceTimerRef = React.useRef(null);
    var closedRef = React.useRef(false);

    function connectEvents() {
      if (closedRef.current) return;
      if (typeof SDK.buildWsUrl !== 'function' || typeof window.WebSocket !== 'function') return;

      SDK.buildWsUrl(API_ROOT + '/events', { since: String(cursorRef.current || 0) }).then(function (url) {
        if (closedRef.current) return;
        var ws;
        try {
          ws = new window.WebSocket(url);
        } catch (e) {
          scheduleReconnect();
          return;
        }
        wsRef.current = ws;

        ws.onopen = function () {
          reconnectDelayRef.current = 1000;
        };

        ws.onmessage = function (event) {
          var data;
          try {
            data = JSON.parse(event.data);
          } catch (e) {
            return;
          }
          if (!data || !Array.isArray(data.events) || data.events.length === 0) return;
          if (typeof data.cursor !== 'number' || data.cursor < 0) return;
          var valid = data.events.every(function (ev) {
            return ev && Array.isArray(ev.committed_records);
          });
          if (!valid) return;
          cursorRef.current = Math.max(cursorRef.current, data.cursor);
          scheduleReload();
        };

        ws.onclose = function (event) {
          wsRef.current = null;
          if (event && event.code === 1008) {
            set(function (prev) {
              return {
                loading: false,
                error: 'Live updates unavailable: authorization failed. Please refresh the page or re-authenticate.',
                handshake: prev.handshake,
                board: prev.board,
                boardEnvelope: prev.boardEnvelope,
                mismatch: false,
                projects: prev.projects,
                detailLoading: prev.detailLoading,
                detailError: prev.detailError,
                detailData: prev.detailData
              };
            });
            return;
          }
          scheduleReconnect();
        };

        ws.onerror = function () {};
      }).catch(function () {
        scheduleReconnect();
      });
    }

    function scheduleReconnect() {
      if (closedRef.current || reconnectTimerRef.current) return;
      reconnectTimerRef.current = setTimeout(function () {
        reconnectTimerRef.current = null;
        connectEvents();
      }, reconnectDelayRef.current);
      reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, 30000);
    }

    function scheduleReload() {
      if (debounceTimerRef.current) return;
      debounceTimerRef.current = setTimeout(function () {
        debounceTimerRef.current = null;
        load();
      }, 250);
    }

    React.useEffect(function () {
      closedRef.current = false;
      load();
      connectEvents();
      return function () {
        closedRef.current = true;
        if (wsRef.current) {
          wsRef.current.close();
          wsRef.current = null;
        }
        if (reconnectTimerRef.current) {
          clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = null;
        }
        if (debounceTimerRef.current) {
          clearTimeout(debounceTimerRef.current);
          debounceTimerRef.current = null;
        }
      };
    }, []);

    if (s.loading) {
      return h('div', { className: 'adrian-kanban-dashboard adrian-kanban-loading' },
        h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-info' },
          'Loading Initiative Tracker…'));
    }

    if (s.error) {
      return h('div', { className: 'adrian-kanban-dashboard adrian-kanban-error' },
        h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-error' },
          'Fetch failed: ' + s.error),
        h('button', { className: 'adrian-kanban-refresh', onClick: load }, 'Retry'));
    }

    var handshake = s.handshake || {};
    var versions = handshake.versions || {};
    var boardEnvelope = s.boardEnvelope;
    var boardAccepted = !!(boardEnvelope && boardEnvelope.result === 'ACCEPTED');
    var board = boardAccepted && boardEnvelope.value ? boardEnvelope.value : {};
    // Derived strictly from the accepted board envelope, after validation.
    var initiatives = Array.isArray(board.initiatives) ? board.initiatives : [];
    var tasks = Array.isArray(board.tasks) ? board.tasks : [];

    var controlsEnabled = handshake.authority === 'adrian-kanban' &&
      String(versions.protocol_version) === EXPECTED_PROTOCOL_VERSION &&
      handshake.mutation_controls_enabled === true;

    var initiativeById = {};
    initiatives.forEach(function (r) {
      if (r && r.initiative_id != null) initiativeById[r.initiative_id] = r;
    });

    // Board positioning: drop closed tasks FIRST; an open task is shown only
    // when its normalized display phase equals its parent initiative's current
    // phase. Closed tasks remain reachable through the initiative detail.
    var byPhase = {};
    var unpositioned = [];
    PHASES.forEach(function (p) { byPhase[p] = { initiatives: [], tasks: [] }; });
    initiatives.forEach(function (r) {
      var p = phaseOf(r);
      if (p) byPhase[p].initiatives.push(r);
      else unpositioned.push({ kind: 'initiative', record: r });
    });
    tasks.forEach(function (r) {
      if (!isTaskOpen(r)) return;
      var p = phaseOf(r);
      if (!p) {
        unpositioned.push({ kind: 'task', record: r });
        return;
      }
      var parent = r.initiative_id != null ? initiativeById[r.initiative_id] : null;
      var parentPhase = parent ? phaseOf(parent) : null;
      if (parentPhase == null) {
        // No parent initiative to verify against: keep a single diagnostic
        // rather than one red line per record.
        if (!unpositioned.some(function (u) {
          return u.kind === 'task' && u.record && u.record.task_id === r.task_id;
        })) {
          unpositioned.push({ kind: 'task', record: r });
        }
        return;
      }
      if (parentPhase !== p) return;
      byPhase[p].tasks.push(r);
    });

    return h('div', { className: 'adrian-kanban-dashboard' },
      h('header', { className: 'adrian-kanban-toolbar' },
        h('h1', null, 'Initiative Tracker'),
        h('select', {
          className: 'adrian-kanban-project-selector',
          'aria-label': 'Project',
          value: selectedBoard,
          onChange: function (event) {
            var value = event.target.value;
            setSelectedBoard(value);
            selectedBoardRef.current = value;
            closeInitiativeDetail();
            load(value);
          }
        },
          h('option', { value: '' }, 'All projects'),
          s.projects.map(function (project) {
            return h('option', { key: project.board, value: project.board }, project.name);
          })
        ),
        h('button', { className: 'adrian-kanban-refresh', onClick: function () { load(); } }, 'Refresh')
      ),
      !boardAccepted ? h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-error' },
        'Board boundary REJECTED or malformed: no board data is available') : null,
      !controlsEnabled ? h('div', { className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-warn adrian-kanban-controls-disabled' },
        'Mutation controls are disabled: handshake authority or protocol version does not match') : null,
      boardAccepted ? h(Diagnostics, { board: board }) : null,
      unpositioned.length ? h('div', { className: 'adrian-kanban-diagnostics' },
        unpositioned.map(function (item, i) {
          var id = item.kind === 'initiative' ? item.record.initiative_id : item.record.task_id;
          return h('div', { key: 'u-' + i, className: 'adrian-kanban-diagnostic adrian-kanban-diagnostic-error' },
            (item.kind === 'initiative' ? 'initiative_id' : 'task_id') + ': ' + (id != null ? id : '(unknown)') +
            ' — Missing or unrecognized lifecycle phase');
        })
      ) : null,
      h('div', { className: 'adrian-kanban-columns' },
        PHASES.map(function (p) {
          return h(Column, {
            key: p,
            phase: p,
            initiatives: byPhase[p].initiatives,
            tasks: byPhase[p].tasks,
            onOpenDetail: openInitiativeDetail
          });
        })
      ),
      openInitiative != null ? h(InitiativeDetail, {
        data: s.detailData,
        loading: s.detailLoading,
        error: s.detailError,
        onClose: closeInitiativeDetail
      }) : null
    );
  }

  function AdrianKanbanPage() {
    return h(App);
  }

  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("adrian-kanban", AdrianKanbanPage);
  }
})();
