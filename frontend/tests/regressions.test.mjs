import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";
import React from "react";

const require = createRequire(import.meta.url);
// Small component harness: execute event handlers and rerender with retained hook state.
function harness(entry, globals = {}) {
  const states = [], refs = [], cache = new Map();
  let stateIndex = 0, refIndex = 0;
  const hooks = { ...React,
    useState(initial) {
      const index = stateIndex++;
      if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial;
      return [states[index], value => { states[index] = typeof value === "function" ? value(states[index]) : value; }];
    },
    useRef(initial) { const index = refIndex++; return refs[index] ??= { current: initial }; },
    useMemo: fn => fn(), useEffect() {},
  };
  function load(file) {
    if (cache.has(file)) return cache.get(file).exports;
    const module = { exports: {} }; cache.set(file, module);
    const code = ts.transpileModule(readFileSync(file, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const localRequire = name => {
      if (name === "react") return hooks;
      if (!name.startsWith(".")) return require(name);
      const path = resolve(dirname(file), name);
      return load([".ts", ".tsx"].map(ext => path + ext).find(existsSync));
    };
    vm.runInNewContext(code, { module, exports: module.exports, require: localRequire,
      console, AbortController, DOMException, FormData, ...globals }, { filename: file });
    return module.exports;
  }
  const exports = load(resolve(entry));
  return { states, render(name, props) { stateIndex = refIndex = 0; return exports[name](props); } };
}
function nodes(tree) {
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (!tree || typeof tree !== "object") return [];
  return [tree, ...nodes(tree.props?.children)];
}
const photo = id => ({ id, filename: "same.jpg", path: "same.jpg", thumbnail: null });
const report = {
  images: [photo("a"), photo("b"), photo("c")], summary: { processed: 3 },
  quality: [{ path: "same.jpg", warnings: ["possibly_blurry"] }],
  groups: [{ id: "g", type: "exact_duplicate", member_photo_ids: ["a", "b"], member_paths: ["same.jpg"], needs_review: true, removal_candidate_ids: [] }],
};
test("quality filter includes grouped images and excludes photos without warnings", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  h.states[0] = "quality";
  let tree = h.render("ReviewResults", { report, onBack() {} });
  assert.equal(nodes(tree).filter(n => n.type === "article").length, 3);
  tree = h.render("ReviewResults", { report: { ...report, quality: [{ path: "same.jpg", warnings: [] }] }, onBack() {} });
  assert.equal(nodes(tree).filter(n => n.type === "article").length, 0);
  assert.ok(nodes(tree).some(n => n.props?.className === "empty-state"));
});
test("same-named ungrouped photo remains visible in all results", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  const tree = h.render("ReviewResults", { report, onBack() {} });
  assert.equal(nodes(tree).filter(n => n.type === "article").length, 3);
});
test("cancelling between polls immediately clears busy state and scheduled polling", async () => {
  let cleared = false;
  const requests = [];
  const h = harness("src/pages/Home.tsx", {
    window: { setTimeout: () => 42, clearTimeout: id => { cleared = id === 42; } },
    fetch: async (url) => {
      requests.push(url);
      return { ok: true, json: async () => ({ job_id: "job", status: "running", progress_percent: 10, processed_images: 1, total_images: 2, current_stage: "Analyzing" }) };
    },
  });
  const props = { report: null, onComplete() {} };
  let tree = h.render("Home", props);
  const modal = nodes(tree).find(n => n.props?.onImportStarted);
  modal.props.onImportStarted("job");
  await new Promise(resolve => setImmediate(resolve));
  tree = h.render("Home", props);
  const cancel = nodes(tree).find(n => n.type === "button" && n.props.children === "Cancel");
  assert.ok(cancel);
  await cancel.props.onClick();
  assert.equal(h.states[1], false);
  assert.equal(h.states[3], "Analysis cancelled.");
  assert.equal(cleared, true);
  assert.ok(requests.includes("/api/jobs/job/cancel"));
});

test("cancelling during job creation cancels the eventual server job", async () => {
  let finishUpload;
  const requests = [];
  const h = harness("src/pages/Home.tsx", {
    window: { setTimeout: () => 42, clearTimeout() {} },
    fetch: async url => {
      requests.push(url);
      if (url === "/api/analyze/jobs") {
        return new Promise(resolve => { finishUpload = () => resolve({ ok: true, json: async () => ({ job_id: "late-job" }) }); });
      }
      return { ok: true, json: async () => ({ status: "cancelled" }) };
    },
  });
  const props = { report: null, onComplete() { assert.fail("cancelled job completed"); } };
  h.states[0] = [new Blob(["photo"], { type: "image/jpeg" })];
  let tree = h.render("Home", props);
  const pending = nodes(tree).find(n => n.props?.className === "analyze-button").props.onClick();
  tree = h.render("Home", props);
  await nodes(tree).find(n => n.type === "button" && n.props.children === "Cancel").props.onClick();
  finishUpload();
  await pending;
  assert.equal(h.states[1], false);
  assert.ok(requests.includes("/api/jobs/late-job/cancel"));
  assert.ok(!requests.includes("/api/jobs/late-job"));
});

const automaticReport = {
  ...report,
  groups: [{ ...report.groups[0], needs_review: false, keeper_photo_id: "a", recommended_keeper_id: "a", removal_candidate_ids: ["b"] }],
};
test("choosing a different duplicate keeper swaps the selected copy", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  const props = { report: automaticReport, onBack() {} };
  let tree = h.render("ReviewResults", props);
  const articles = nodes(tree).filter(n => n.type === "article");
  const second = articles.find(n => n.key === "b");
  nodes(second).find(n => n.props?.className === "keeper-button").props.onClick();
  tree = h.render("ReviewResults", props);
  assert.equal(h.states[1].keepers.get("g"), "b");
  assert.ok(h.states[1].selected.has("a"));
  assert.ok(!h.states[1].selected.has("b"));
  const keeper = nodes(tree).find(n => n.type === "article" && n.key === "b");
  assert.equal(nodes(keeper).find(n => n.props?.className === "check-button").props.disabled, true);
});
test("choosing a keeper in similar shots never preselects other moments", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  const similarReport = { ...report, groups: [{ ...report.groups[0], type: "similar_only", needs_review: true, recommended_keeper_id: "a" }] };
  const tree = h.render("ReviewResults", { report: similarReport, onBack() {} });
  nodes(tree).find(n => n.props?.className === "keeper-button").props.onClick();
  assert.equal(h.states[1].selected.size, 0);
  assert.equal(h.states[1].keepers.get("g"), "a");
});
test("export downloads an accurate manifest without image contents", async () => {
  let downloaded, clicked = false;
  const link = { click() { clicked = true; }, remove() {} };
  const h = harness("src/pages/ReviewResults.tsx", {
    Blob,
    URL: { createObjectURL(blob) { downloaded = blob; return "blob:test"; }, revokeObjectURL() {} },
    document: { createElement: () => link, body: { appendChild() {} } },
    window: { setTimeout() {} },
  });
  const tree = h.render("ReviewResults", { report: automaticReport, onBack() {} });
  nodes(tree).find(n => n.props?.className === "export-button").props.onClick();
  const manifest = JSON.parse(await downloaded.text());
  assert.equal(clicked, true);
  assert.equal(link.download, "pixsort-removal-selection.json");
  assert.deepEqual(manifest.selected_for_removal.map(photo => photo.id), ["b"]);
  assert.equal(manifest.originals_modified, false);
  assert.ok(!("thumbnail" in manifest.selected_for_removal[0]));
});
test("quality filtering distinguishes identical filenames by photo ID", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  h.states[0] = "quality";
  const tree = h.render("ReviewResults", { report: { ...report, quality: [{ path: "same.jpg", photo_id: "c", warnings: ["low_resolution"] }] }, onBack() {} });
  const cards = nodes(tree).filter(n => n.type === "article");
  assert.equal(cards.length, 1);
  assert.equal(cards[0].key, "c");
});
test("comparison opens with the keeper first and two distinct photos", () => {
  const h = harness("src/pages/ReviewResults.tsx");
  const tree = h.render("ReviewResults", { report: automaticReport, onBack() {} });
  nodes(tree).find(n => n.props?.className === "compare-button").props.onClick();
  assert.equal(h.states[2].groupId, "g");
  assert.deepEqual(Array.from(h.states[2].ids), ["a", "b"]);
});

test("reset is available after selecting photos and cancels the active batch", async () => {
  let resets = 0;
  const calls = [];
  const h = harness("src/pages/Home.tsx", {
    window: { setTimeout: () => 42, clearTimeout() {} },
    fetch: async url => { calls.push(url); return { ok: true, json: async () => ({ job_id: "reset-job", status: "running", progress_percent: 0, processed_images: 0, total_images: 1, current_stage: "Preparing" }) }; },
  });
  const props = { report: null, onComplete() { assert.fail("reset batch completed"); }, onReset() { resets++; } };
  let tree = h.render("Home", props);
  assert.equal(nodes(tree).find(n => n.props?.className === "reset-batch-button").props.disabled, true);
  nodes(tree).find(n => n.props?.onImportStarted).props.onImportStarted("reset-job");
  await new Promise(resolve => setImmediate(resolve));
  tree = h.render("Home", props);
  nodes(tree).find(n => n.props?.className === "reset-batch-button").props.onClick();
  assert.equal(resets, 1);
  assert.equal(h.states[1], false);
  assert.ok(calls.includes("/api/jobs/reset-job/cancel"));
});

test("reset clears the report and returns the app to an empty library", () => {
  const h = harness("src/App.tsx");
  h.states[0] = "home"; h.states[1] = automaticReport;
  const tree = h.render("App", {});
  nodes(tree).find(n => n.props?.onReset).props.onReset();
  const resetTree = h.render("App", {});
  assert.equal(h.states[0], "home");
  assert.equal(h.states[1], null);
  assert.equal(h.states[2], 1);
  assert.equal(nodes(resetTree).find(n => n.props?.onComplete)?.props.report, null);
  assert.ok(!nodes(resetTree).some(n => n.props?.onBack));
});
