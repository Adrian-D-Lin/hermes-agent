"""Static release contract for the plugin-owned lifecycle dashboard assets."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1] / "plugins" / "adrian-kanban" / "dashboard"


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
    assert 'api("/handshake")' in javascript
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
let state;
let registered;
const effects = [];
const calls = [];
const React = {
  createElement: function (type, props) {
    return {type: type, props: props || {}, children: Array.prototype.slice.call(arguments, 2)};
  },
  useState: function (initial) {
    if (state === undefined) state = initial;
    return [state, function (next) { state = typeof next === "function" ? next(state) : next; }];
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
  "/api/plugins/adrian-kanban/board": {
    result: "ACCEPTED",
    value: {
      initiatives: [{initiative_id: "initiative-probe", title: "Probe initiative", current_phase: "D2", current_segment_id: null}],
      tasks: [{task_id: "task-probe", title: "Probe task", lifecycle_phase: "DEV2", segment_id: "S1", status: "ready"}]
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
page.type(page.props);
if (effects.length < 1) process.exit(12);
effects.splice(0).forEach(function (effect) { effect(); });
setImmediate(function () {
  setImmediate(function () {
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
    if (calls.join("|") !== "/api/plugins/adrian-kanban/handshake|/api/plugins/adrian-kanban/board") process.exit(13);
    if (!text.includes("Probe initiative") || !text.includes("Probe task") || !text.includes("ready") || !text.includes("S1")) process.exit(14);
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
    assert ".adrian-kanban-task-status" in css
    assert ".adrian-kanban-diagnostic" in css
    assert ".adrian-kanban-controls-disabled" in css
