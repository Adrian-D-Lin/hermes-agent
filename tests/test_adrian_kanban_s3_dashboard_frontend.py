"""Static release contract for the plugin-owned lifecycle dashboard assets."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1] / "plugins" / "adrian-kanban" / "dashboard"
DESKTOP_ENTRY = ROOT.parent / "desktop" / "plugin.js"


def _asset(relative: str) -> str:
    return ROOT.joinpath(relative).read_text(encoding="utf-8")


def test_dashboard_dist_is_reproducible_from_maintained_source():
    assert _asset("src/index.js") == _asset("dist/index.js")
    assert _asset("src/style.css") == _asset("dist/style.css")


def test_dashboard_defines_only_the_nine_lifecycle_phase_columns():
    javascript = _asset("src/index.js")
    match = re.search(r"const PHASES = Object\.freeze\((\[[^;]+\])\);", javascript)
    assert match is not None
    assert json.loads(match.group(1)) == [
        "D1",
        "D2",
        "D3",
        "D4",
        "DEV1",
        "DEV2",
        "DEV3",
        "DEV4",
        "PC1",
    ]


def test_dashboard_checks_protocol_before_enabling_mutation_controls():
    javascript = _asset("src/index.js")

    assert 'const EXPECTED_PROTOCOL_VERSION = "2"' in javascript
    assert "/handshake" in javascript
    assert "handshake.versions" in javascript
    assert "mutation_controls_enabled" in javascript
    assert "protocol_version" in javascript
    assert "controlsEnabled" in javascript
    assert "disabled" in javascript


def test_dashboard_renders_initiative_phase_and_subordinate_task_status():
    javascript = _asset("src/index.js")

    assert "initiative_id" in javascript
    assert "current_phase" in javascript
    assert "current_segment_id" in javascript
    assert "task_id" in javascript
    assert "status" in javascript
    assert "task-status" in javascript


def test_dashboard_nests_compact_tasks_and_opens_task_detail():
    javascript = _asset("src/index.js")
    desktop = DESKTOP_ENTRY.read_text(encoding="utf-8")

    for source in (javascript, desktop):
        assert "function TaskRow" in source
        assert "nestedTasks" in source
        assert "task.initiative_id ===" in source
        assert "exactStepOf(record)" in source
        assert "'|'" in source
        assert "function TaskDetail" in source
        assert "Comment history (" in source
        assert "'/tasks/' + encodeURIComponent(id)" in source
        assert "onOpenTaskDetail" in source

    assert "adrian-kanban-nested-tasks" in javascript
    assert "adrian-kanban-task-row" in javascript
    assert "h(Card, { key: 't-'" not in javascript


def test_task_status_badge_matches_phase_badge_colours():
    css = _asset("src/style.css")
    phase = re.search(r"\.adrian-kanban-badge-phase\s*\{(?P<body>[^}]*)\}", css)
    status = re.search(r"\.adrian-kanban-task-status\s*\{(?P<body>[^}]*)\}", css)

    assert phase is not None
    assert status is not None
    for declaration in (
        "background: var(--color-muted, #e7f1ff)",
        "color: var(--color-primary, #1a4f8b)",
    ):
        assert declaration in phase.group("body")
        assert declaration in status.group("body")

    desktop = DESKTOP_ENTRY.read_text(encoding="utf-8")
    assert "taskStatus: { fontSize: 11, padding: '2px 6px', borderRadius: 4, background: '#e7f1ff', color: '#1a4f8b' }" in desktop


def test_dashboard_surfaces_actionable_boundary_diagnostics():
    javascript = _asset("src/index.js")

    assert "failed_checks" in javascript
    assert "remediation" in javascript
    assert "not_evaluated_checks" in javascript
    assert "REJECTED" in javascript


def test_dashboard_registers_only_as_the_plugin_owned_surface():
    javascript = _asset("dist/index.js")

    assert (
        '__HERMES_PLUGINS__.register("adrian-kanban", AdrianKanbanPage);'
        in javascript
    )
    assert "const React = SDK.React" in javascript
    assert "React.render" not in javascript
    assert "document." not in javascript
    assert javascript.index("if (!SDK") < javascript.index("const React = SDK.React")
    assert "/api/plugins/adrian-kanban" in javascript


def test_dashboard_unwraps_boundary_and_does_not_offer_fake_mutations():
    javascript = _asset("src/index.js")

    assert "boardEnvelope.value" in javascript
    assert "Move" not in javascript
    assert "Edit" not in javascript


def test_dashboard_does_not_discard_unpositioned_cards():
    javascript = _asset("src/index.js")

    assert "unpositioned" in javascript
    assert "Missing or unrecognized lifecycle phase" in javascript
    assert "No cards" in javascript


def test_dashboard_uses_authenticated_plugin_events_for_live_refresh():
    javascript = _asset("src/index.js")

    assert "SDK.buildWsUrl" in javascript
    assert "API_ROOT + '/events'" in javascript
    assert "new window.WebSocket" in javascript
    assert "committed_records" in javascript
    assert "scheduleReload" in javascript
    assert "closedRef.current || reconnectTimerRef.current" in javascript
    assert "wsRef.current.close()" in javascript


def test_dashboard_executes_in_host_component_contract_and_keeps_text_utf8():
    javascript_path = ROOT / "src" / "index.js"
    javascript = javascript_path.read_text(encoding="utf-8")
    assert "â" not in javascript

    probe = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const states = [];
let stateIndex = 0;
let registered;
const effects = [];
const calls = [];
const React = {
  createElement: function (type, props) {
    return {type: type, props: props || {}, children: Array.prototype.slice.call(arguments, 2)};
  },
  useState: function (initial) {
    const index = stateIndex++;
    if (states[index] === undefined) states[index] = initial;
    return [states[index], function (next) {
      states[index] = typeof next === "function" ? next(states[index]) : next;
    }];
  },
  useRef: function (initial) { return {current: initial}; },
  useEffect: function (effect) { effects.push(effect); }
};
const responses = {
  "/api/plugins/adrian-kanban/handshake": {
    authority: "adrian-kanban",
    versions: {protocol_version: "2"},
    mutation_controls_enabled: true
  },
  "/api/plugins/adrian-kanban/projects": {
    projects: [{id: "p_probe", slug: "probe", name: "Probe", board: "probe"}]
  },
  "/api/plugins/adrian-kanban/board": {
    result: "ACCEPTED",
    value: {
      initiatives: [{initiative_id: "initiative-probe", title: "Probe initiative", current_phase: "DEV2", current_segment_id: "S1"}],
      tasks: [
        {task_id: "task-probe", initiative_id: "initiative-probe", title: "Probe task", lifecycle_phase: "DEV2.1", segment_id: "S1", status: "ready"},
        {task_id: "task-done", initiative_id: "initiative-probe", title: "Historical D1 task", status: "done"}
      ]
    }
  }
};
const windowObject = {
  __HERMES_PLUGIN_SDK__: {
    React: React,
    fetchJSON: function (url) { calls.push(url); return Promise.resolve(responses[url]); }
  },
  __HERMES_PLUGINS__: {
    register: function (name, component) { registered = {name: name, component: component}; }
  }
};
vm.runInNewContext(source, {window: windowObject});
if (!registered || registered.name !== "adrian-kanban" || typeof registered.component !== "function") process.exit(10);
const page = registered.component();
if (!page || typeof page.type !== "function") process.exit(11);
stateIndex = 0;
page.type(page.props);
if (effects.length < 1) process.exit(12);
effects.splice(0).forEach(function (effect) { effect(); });
setImmediate(function () {
  setImmediate(function () {
    stateIndex = 0;
    const tree = page.type(page.props);
    function flatten(value, output) {
      if (value == null || value === false) return;
      if (Array.isArray(value)) { value.forEach(function (item) { flatten(item, output); }); return; }
      if (typeof value === "string" || typeof value === "number") { output.push(String(value)); return; }
      if (typeof value.type === "function") { flatten(value.type(value.props || {}), output); return; }
      flatten(value.children, output);
    }
    const output = [];
    flatten(tree, output);
    const text = output.join(" ");
    if (calls.join("|") !== "/api/plugins/adrian-kanban/handshake|/api/plugins/adrian-kanban/projects|/api/plugins/adrian-kanban/board") process.exit(13);
    if (!text.includes("Probe initiative") || !text.includes("DEV2.1") || !text.includes("ready") || !text.includes("S1")) process.exit(14);
    if (text.includes("Probe task")) process.exit(18);
    if (text.includes("Historical D1 task") || text.includes("task-done") || text.includes("task_id: task-done")) process.exit(17);
    function findType(value, type) {
      if (value == null || value === false) return null;
      if (Array.isArray(value)) {
        for (const item of value) {
          const found = findType(item, type);
          if (found) return found;
        }
        return null;
      }
      if (value.type === type) return value;
      if (typeof value.type === "function") return findType(value.type(value.props || {}), type);
      return findType(value.children, type);
    }
    const selector = findType(tree, "select");
    if (!selector || typeof selector.props.onChange !== "function") process.exit(15);
    selector.props.onChange({target: {value: "probe"}});
    const switched = calls.slice(-3).join("|");
    if (switched !== "/api/plugins/adrian-kanban/handshake|/api/plugins/adrian-kanban/projects|/api/plugins/adrian-kanban/board?board=probe") process.exit(16);
  });
});
"""
    result = subprocess.run(
        ["node", "-e", probe, str(javascript_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_dashboard_styles_support_phase_columns_and_status_badges():
    css = _asset("src/style.css")

    assert ".adrian-kanban-columns" in css
    assert "overflow-x" in css
    assert "min-width: 0" in css
    assert "width: 100%" in css
    assert "flex: 0 0 240px" in css
    assert ".adrian-kanban-task-status" in css
    assert ".adrian-kanban-diagnostic" in css
    assert ".adrian-kanban-controls-disabled" in css


def test_segment_badges_match_phase_badge_colours_on_web_and_desktop():
    css = _asset("src/style.css")
    phase = re.search(
        r"\.adrian-kanban-badge-phase\s*\{(?P<body>[^}]*)\}", css
    )
    segment = re.search(
        r"\.adrian-kanban-badge-segment\s*\{(?P<body>[^}]*)\}", css
    )

    assert phase is not None
    assert segment is not None
    assert phase.group("body").strip() == segment.group("body").strip()
    assert "segment ? h('span', { style: styles.badgePhase }" in DESKTOP_ENTRY.read_text(
        encoding="utf-8"
    )
    assert "? h('span', { style: styles.badgePhase }, 'SEG '" in DESKTOP_ENTRY.read_text(
        encoding="utf-8"
    )


def test_dashboard_uses_initiative_tracker_title_and_project_board_selector():
    javascript = _asset("src/index.js")
    manifest = json.loads(_asset("manifest.json"))

    assert manifest["label"] == "Initiative Tracker"
    assert "Initiative Tracker" in javascript
    assert "/projects" in javascript
    assert "encodeURIComponent" in javascript
    assert "All projects" in javascript
    assert "project-selector" in javascript


def test_dashboard_treats_board_as_current_work_and_detail_as_history():
    javascript = _asset("src/index.js")

    assert "CLOSED_TASK_STATUSES" in javascript
    assert "if (!isTaskOpen(r)) return" in javascript
    assert "if (parentPhase !== p) return" in javascript
    assert "ctx.rest" not in javascript
    assert "api('/initiatives/'" in javascript
    assert "'?board=' + encodeURIComponent(board)" in javascript
    assert "envelope.result === 'ACCEPTED'" in javascript
    assert "envelope.value" in javascript
    assert "Transition history (" in javascript
    assert "phaseOf(t) || legacyPhaseOf(t) || 'Legacy/Unclassified'" in javascript
    assert "Array.isArray(d.attachments)" in javascript
    assert "h('details'" in javascript
    assert "h('h3', null, 'Transition history (" in javascript


def test_dashboard_phase_parsing_is_anchored_and_legacy_tokens_are_bounded():
    javascript = _asset("src/index.js")

    assert r"(\.[0-9A-Z]+)?$/" in javascript
    assert r"\b(?:DEV[1-4]|D[1-4]|PC1)\b" in javascript


def test_dashboard_styles_give_cards_and_details_explicit_contrast():
    css = _asset("src/style.css")

    for selector in (
        ".adrian-kanban-card",
        ".adrian-kanban-detail-panel",
        ".adrian-kanban-detail-section",
        ".adrian-kanban-task-entry",
        ".adrian-kanban-attachment",
    ):
        assert selector in css
    assert "background: #0b2545" in css
    assert "color: var(--color-foreground, #" in css

    column_rule = re.search(r"\.adrian-kanban-column\s*\{([^}]+)\}", css, re.DOTALL)
    assert column_rule is not None
    assert "background: var(--color-background," in column_rule.group(1)

    detail_rule = re.search(r"\.adrian-kanban-detail-panel\s*\{([^}]+)\}", css, re.DOTALL)
    assert detail_rule is not None
    assert "background: #0b2545" in detail_rule.group(1)
    assert "user-select: text" in detail_rule.group(1)

    selector_rule = re.search(
        r"\.adrian-kanban-project-selector\s*\{([^}]+)\}", css, re.DOTALL
    )
    assert selector_rule is not None
    assert "background: #102f57" in selector_rule.group(1)
    assert "color: var(--color-foreground, #f8fafc)" in selector_rule.group(1)

    assert ".adrian-kanban-refresh," in css
    assert ".adrian-kanban-mutation" in css
    assert "background: #102f57" in css
    assert "background: #163b68" in css

    close_rule = re.search(
        r"\.adrian-kanban-detail-close\s*\{([^}]+)\}", css, re.DOTALL
    )
    assert close_rule is not None
    assert "background: #102f57" in close_rule.group(1)
    assert "color: var(--color-foreground, #f8fafc)" in close_rule.group(1)


def test_dashboard_refresh_preserves_open_detail_and_project_change_closes_it():
    javascript = _asset("src/index.js")

    assert "function api(path, options)" in javascript
    assert "SDK.fetchJSON(API_ROOT + path, options)" in javascript
    assert javascript.count("detailLoading: prev.detailLoading") >= 2
    assert javascript.count("detailData: prev.detailData") >= 2
    assert "Initiative detail is unavailable; close and reopen the initiative." in javascript
    assert "closeInitiativeDetail();" in javascript
    assert "loading: prev.boardEnvelope == null" in javascript


def test_dashboard_detail_exposes_the_structured_cold_session_contract():
    javascript = _asset("src/index.js")

    for field in (
        "Phase results (",
        "Segment manifest and readiness",
        "Workspace assignments (",
        "Open findings (",
        "Next permitted routes (",
        "lifecycle_contract",
        "accepted_handoff",
        "latest_candidate",
    ):
        assert field in javascript

    assert "key: 'h-' + i, className: 'adrian-kanban-detail-record'" in javascript


def test_unified_package_supplies_the_matching_desktop_extension():
    javascript = DESKTOP_ENTRY.read_text(encoding="utf-8")

    assert "@hermes/plugin-sdk" in javascript
    assert "id: 'adrian-kanban'" in javascript
    assert "name: 'Initiative Tracker'" in javascript
    assert "label: 'Initiative Tracker'" in javascript
    assert "defaultEnabled: false" in javascript
    assert "ROUTES_AREA" in javascript
    assert "SIDEBAR_NAV_AREA" in javascript
    assert "path: '/kanban'" in javascript
    assert "ctx.rest" in javascript
    assert "'/handshake'" in javascript
    assert "'/projects'" in javascript
    assert "/board" in javascript
    assert "encodeURIComponent" in javascript
    assert "All projects" in javascript
    assert "EXPECTED_PROTOCOL_VERSION = '2'" in javascript
    assert "mutation_controls_enabled" in javascript
    assert "/api/plugins/kanban" not in javascript


def test_desktop_extension_renders_the_complete_lifecycle_projection_and_diagnostics():
    javascript = DESKTOP_ENTRY.read_text(encoding="utf-8")

    phase_match = re.search(
        r"const PHASES = Object\.freeze\((\[[^\]]+\])\)", javascript
    )
    assert phase_match is not None
    assert json.loads(phase_match.group(1).replace("'", '"')) == [
        "D1",
        "D2",
        "D3",
        "D4",
        "DEV1",
        "DEV2",
        "DEV3",
        "DEV4",
        "PC1",
    ]
    for field in (
        "initiative_id",
        "task_id",
        "current_phase",
        "lifecycle_phase",
        "current_segment_id",
        "segment_id",
        "status",
        "failed_checks",
        "not_evaluated_checks",
        "accepted_format",
        "remediation",
    ):
        assert field in javascript


def test_desktop_extension_matches_current_work_and_history_detail_contract():
    javascript = DESKTOP_ENTRY.read_text(encoding="utf-8")

    assert "CLOSED_TASK_STATUSES" in javascript
    assert "if (!isTaskOpen(record)) return" in javascript
    assert "if (parentPhase !== p) return" in javascript
    assert "ctx.rest('/initiatives/'" in javascript
    assert "'?board=' + encodeURIComponent(board)" in javascript
    assert "envelope.result === 'ACCEPTED'" in javascript
    assert "detailData: envelope.value" in javascript
    assert "phaseOf(t) || legacyPhaseOf(t) || 'Legacy/Unclassified'" in javascript
    assert r"\b(?:DEV[1-4]|D[1-4]|PC1)\b" in javascript
    assert "Array.isArray(d.attachments)" in javascript
    assert "Transition history (" in javascript
    assert "h('details'" in javascript
    assert "background: '#0b2545'" in javascript
    assert "color: 'var(--color-foreground, #" in javascript
    assert "background: 'var(--color-background," in javascript
    assert "userSelect: 'text'" in javascript
    assert "button: { cursor: 'pointer'" in javascript
    assert "background: '#102f57', color: 'var(--color-foreground, #f8fafc)'" in javascript
    assert "key: 'h-' + i, style: styles.detailRecord" in javascript

    for field in (
        "Phase results (",
        "Segment manifest and readiness",
        "Workspace assignments (",
        "Open findings (",
        "Next permitted routes (",
        "lifecycle_contract",
        "accepted_handoff",
        "latest_candidate",
    ):
        assert field in javascript


def test_desktop_refresh_preserves_open_detail_and_project_change_closes_it():
    javascript = DESKTOP_ENTRY.read_text(encoding="utf-8")

    assert javascript.count("detailLoading: prev.detailLoading") >= 2
    assert javascript.count("detailData: prev.detailData") >= 2
    assert "Initiative detail is unavailable; close and reopen the initiative." in javascript
    assert "closeInitiativeDetail()" in javascript
    assert "loading: prev.boardEnvelope == null" in javascript


def test_desktop_extension_is_one_self_contained_runtime_module():
    javascript = DESKTOP_ENTRY.read_text(encoding="utf-8")

    imports = re.findall(r"from\s+['\"]([^'\"]+)['\"]", javascript)
    assert set(imports) == {"@hermes/plugin-sdk", "react"}
    assert "export default plugin" in javascript
    assert "setInterval" in javascript
    assert "clearInterval" in javascript

    parsed = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=javascript.encode("utf-8"),
        capture_output=True,
        timeout=10,
    )
    assert parsed.returncode == 0, (parsed.stderr or parsed.stdout).decode(
        "utf-8", errors="replace"
    )
