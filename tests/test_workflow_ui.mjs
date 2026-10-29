import assert from "node:assert/strict";
import test from "node:test";
import { createWorkflowUI } from "../src/oea/web/static/workflow-canvas.mjs";

test("definition refresh preserves the fitted view and resizing refits without marking edits",async t=>{
  const previous={window:globalThis.window,document:globalThis.document,ResizeObserver:globalThis.ResizeObserver};
  const canvas={clientWidth:700,clientHeight:420,addEventListener(){}},world={style:{}},zoom={};
  let observer;
  globalThis.window={addEventListener(){}};
  globalThis.document={addEventListener(){},querySelector(selector){return ({"#wf-canvas":canvas,"#wf-world":world,"#wf-zoom-label":zoom})[selector]||null;},querySelectorAll(){return [];}};
  globalThis.ResizeObserver=class{constructor(callback){this.callback=callback;observer=this;}observe(){}disconnect(){}};
  t.after(()=>Object.assign(globalThis,previous));
  const graph={nodes:[{id:"start",type:"start",title:"Start",position:{x:80,y:120},config:{input_schema:{type:"object"}}},
    {id:"end",type:"end",title:"End",position:{x:640,y:120},config:{outputs:{}}}],edges:[{id:"edge",source:"start",target:"end",branch:"default"}],viewport:{x:0,y:0,zoom:1}};
  const definition={id:"flow",project_id:"project",name:"Flow",revision:1,state:"active",graph};
  const rpc=async method=>({"workflows.list":{items:[definition],cursor:null},"workflows.get":structuredClone(definition),"workflows.node_types":{},"workflows.activity":{items:[],cursor:null}})[method];
  const ui=createWorkflowUI({state:{route:"workflows",project:{id:"project",name:"Project"},agents:[],sessions:[]},rpc,escape:String,icon:()=>"",badge:String,markdown:String,toast(){},render(){},idempotency:()=>"key"});
  await ui.load("project");ui.mount();
  const fitted=world.style.transform;
  assert.notEqual(fitted,"translate(0px,0px) scale(1)");
  await ui.onEvent({type:"workflow.updated",payload:{project_id:"project",workflow_id:"flow"}});ui.mount();
  assert.equal(world.style.transform,fitted);
  canvas.clientWidth=520;observer.callback([],observer);
  assert.notEqual(world.style.transform,fitted);
  assert.doesNotMatch(ui.page(),/有未保存的修改/);
});

test("run selection survives new executions and definition edits while rendering its saved graph", async t => {
  const oldWindow = globalThis.window, oldDocument = globalThis.document;
  globalThis.window = { addEventListener() {} };
  globalThis.document = { addEventListener() {}, querySelector() { return null; } };
  t.after(() => { globalThis.window = oldWindow; globalThis.document = oldDocument; });
  const graph = { nodes: [
    { id: "start", type: "start", title: "Start", position: { x: 0, y: 0 }, config: { input_schema: { type: "object" } } },
    { id: "end", type: "end", title: "Saved end", position: { x: 300, y: 0 }, config: { outputs: {} } },
  ], edges: [{ id: "edge", source: "start", target: "end", branch: "default" }], viewport: { x: 0, y: 0, zoom: 1 } };
  let definition = { id: "flow", project_id: "project", name: "Flow", revision: 2, state: "active", graph, validation: { valid: true, warnings: [] } };
  const makeRun = (id, created_at, revision) => ({ id, workflow_id: "flow", project_id: "project", session_id: "session", state: "waiting", revision, created_at, graph: structuredClone(graph),
    nodes: [{ node_id: "start", type: "start", title: "Start", state: "completed" },
      { node_id: "end", type: "end", title: "Saved end", state: "PENDING" }] });
  const runs = [makeRun("run-b", 2, 2), makeRun("run-a", 1, 1)];
  const state = { route: "workflows", project: { id: "project", name: "Project" }, session: { id: "session" }, agents: [], sessions: [] };
  const rpc = async (method, params) => {
    const values = {
      "workflows.list": () => ({ items: [definition], cursor: null }),
      "workflows.node_types": () => ({}),
      "workflows.activity": () => ({items:[{ workflow_id: "flow", active_count: runs.length }],cursor:null}),
      "workflows.get": () => structuredClone(definition),
      "workflows.list_runs": () => ({ items: runs.map(({ graph, nodes, ...run }) => run), cursor: null }),
      "workflows.inspect": () => structuredClone(runs.find(run => run.id === params.workflow_run_id)),
    };
    assert.ok(values[method], method);
    return values[method]();
  };
  let ui, html;
  ui = createWorkflowUI({ state, rpc, escape: value => String(value ?? ""), icon: () => "", badge: value => String(value),
    markdown: value => value, toast: () => {}, time: () => "", idempotency: () => "key", render: () => { if (ui) html = ui.page(); } });
  await ui.load("project");
  assert.match(html, /value="run-b" selected/);
  assert.match(html, /等待前置/);
  await ui.change({ id: "wf-run-choice", value: "run-a" });
  definition = structuredClone(definition);
  definition.revision = 3;
  definition.graph.nodes[1].title = "New definition end";
  runs.unshift(makeRun("run-c", 3, 3));
  await ui.onEvent({ type: "workflow.updated", payload: { project_id: "project", workflow_id: "flow" } });
  await ui.refresh();
  assert.match(html, /value="run-a" selected/);
  assert.match(html, /当前 r1/);
  assert.match(html, /已保存 · r3/);
  assert.match(html, /aria-label="Saved end"/);
  assert.doesNotMatch(html, /aria-label="New definition end"/);
  assert.match(html, /value="run-c"/);
});
