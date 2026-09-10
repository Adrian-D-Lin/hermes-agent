(function () {
  'use strict';

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const API_ROOT = '/api/plugins/adrian-kanban';
  const PHASES = Object.freeze(["D1", "D2", "D3", "D4", "DEV1", "DEV2", "DEV3", "DEV4", "PC1"]);
  const EXPECTED_PROTOCOL_VERSION = "2";

  function api(path) {
    return SDK.fetchJSON(API_ROOT + path);
  }

  function h() {
    var args = Array.prototype.slice.call(arguments);
    var tag = args.shift();
    var props = args.length && args[0] != null && typeof args[0] === 'object' && !Array.isArray(args[0]) ? args.shift() : null;
    return React.createElement(tag, props, args.length ? args : undefined);
  }

  function phaseOf(record) {
    if (!record || typeof record !== 'object') return null;
    var raw = record.current_phase != null ? record.current_phase : record.lifecycle_phase;
    if (raw == null) return null;
    var p = String(raw).toUpperCase();
    return PHASES.indexOf(p) >= 0 ? p : null;
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

  function titleOf(record) {
    if (!record || typeof record !== 'object') return '';
    return record.title != null ? String(record.title) : (record.name != null ? String(record.name) : '');
  }

  function Card(props) {
    var record = props.record;
    var kind = props.kind;
    var phase = phaseOf(record);
    var segment = segmentOf(record);
    var status = nativeStatusOf(record);
    var id = kind === 'initiative' ? record.initiative_id : record.task_id;
    var title = titleOf(record) || (kind === 'initiative' ? 'Untitled initiative' : 'Untitled task');
    var invalid = !phase;
    return h('div', { className: 'adrian-kanban-card' + (invalid ? ' adrian-kanban-card-invalid' : '') },
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
          return h(Card, { key: 'i-' + i, record: r, kind: 'initiative' });
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

  function App() {
    var state = React.useState({
      loading: true,
      error: null,
      handshake: null,
      board: null,
      boardEnvelope: null,
      mismatch: false,
      projects: []
    });
    var s = state[0];
    var set = state[1];
    var selectedBoardState = React.useState('');
    var selectedBoard = selectedBoardState[0];
    var setSelectedBoard = selectedBoardState[1];
    var selectedBoardRef = React.useRef('');

    function load(boardOverride) {
      set(function (prev) {
        return {
          loading: true,
          error: null,
          handshake: prev.handshake,
          board: prev.board,
          boardEnvelope: prev.boardEnvelope,
          mismatch: false,
          projects: prev.projects
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
        if (boardEnvelope && boardEnvelope.result === 'ACCEPTED') {
          board = boardEnvelope.value;
        }
        set(function () {
          return {
            loading: false,
            error: null,
            handshake: handshake,
            board: board,
            boardEnvelope: boardEnvelope,
            mismatch: false,
            projects: projects
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
            projects: prev.projects
          };
        });
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
                projects: prev.projects
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
    var initiatives = Array.isArray(board.initiatives) ? board.initiatives : [];
    var tasks = Array.isArray(board.tasks) ? board.tasks : [];

    var controlsEnabled = handshake.authority === 'adrian-kanban' &&
      String(versions.protocol_version) === EXPECTED_PROTOCOL_VERSION &&
      handshake.mutation_controls_enabled === true;

    var byPhase = {};
    var unpositioned = [];
    PHASES.forEach(function (p) { byPhase[p] = { initiatives: [], tasks: [] }; });
    initiatives.forEach(function (r) {
      var p = phaseOf(r);
      if (p) byPhase[p].initiatives.push(r);
      else unpositioned.push({ kind: 'initiative', record: r });
    });
    tasks.forEach(function (r) {
      var p = phaseOf(r);
      if (p) byPhase[p].tasks.push(r);
      else unpositioned.push({ kind: 'task', record: r });
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
            tasks: byPhase[p].tasks
          });
        })
      )
    );
  }

  function AdrianKanbanPage() {
    return h(App);
  }

  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("adrian-kanban", AdrianKanbanPage);
  }
})();
