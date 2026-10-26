import { $,escape,icon,rpc,modal,element,notify } from "./ui.mjs";
import { markdown as renderMarkdown } from "./markdown.mjs";
import { createWorkflowUI } from "./workflow-canvas.mjs";
import { editorInteraction } from "./presentation.mjs";

const controllers=new WeakMap();
export async function renderWorkflows(app,root) {
  let entry=controllers.get(app);
  if(!entry) {
    const state={get route(){return app.state.page;},get modal(){return !!$(".v2-dialog-backdrop");}};
    const sync=()=>Object.assign(state,{project:app.state.project,agents:app.state.agents.map(value=>({id:value.definition.id,name:value.definition.name,kind:value.definition.execution.program?"program":"model"})),sessions:app.state.sessions.map(value=>({...value,title:value.name,state:value.closed?"CLOSED":"OPEN"})),session:app.state.session});
    let component;
    const draw=()=>{
      sync();const target=$("#page");if(state.route!=="workflows"||!target||component.isInteracting())return;
      const focused=document.activeElement,key=focused?.dataset.focus||focused?.id,selection=focused?.selectionStart,scrolls=[...target.querySelectorAll(".wf-inspector-body,.wf-library-list")].map(item=>[item.className,item.scrollTop]);
      target.className="";target.innerHTML=component.page();component.mount();
      target.onclick=async event=>{if(editorInteraction(event))return;const control=event.target.closest("[data-action]");if(!control||control.disabled)return;try{if(control.dataset.action==="wf-code-graphs"){const graphs=await rpc("executions.graphs");modal("Python 代码图",element(`<div>${graphs.map(graph=>`<section><h3>${escape(graph.name)} · ${escape(graph.version)}</h3><pre>${escape(JSON.stringify(graph,null,2))}</pre></section>`).join("")}</div>`));}else if(control.dataset.action==="open-run-thread"){const run=await rpc("runs.get",{run_id:control.dataset.id,project_id:state.project.id});const instance=app.state.instances.find(value=>value.id===run.instance_id);if(instance){app.state.selectedInstance=instance;if(!app.state.openTabs.includes(instance.id))app.state.openTabs.push(instance.id);await app.navigate("conversation");}}else await component.action(control.dataset.action,control);}catch(error){notify(error.message,true);}};
      target.oninput=event=>{try{component.input(event.target);}catch(error){notify(error.message,true);}};
      target.onchange=async event=>{try{if(editorInteraction(event))return;if(event.target.tagName==="SELECT"||event.target.type==="checkbox")component.input(event.target);await component.change(event.target);}catch(error){notify(error.message,true);}};
      if(key){const field=[...target.querySelectorAll("input,textarea,select,button")].find(value=>value.dataset.focus===key||value.id===key);if(field){field.focus({preventScroll:true});if(selection!==null&&selection!==undefined&&["text","search","textarea"].includes(field.type))field.setSelectionRange(selection,selection);}}
      for(const [name,top] of scrolls){const item=[...target.querySelectorAll(".wf-inspector-body,.wf-library-list")].find(value=>value.className===name);if(item)item.scrollTop=top;}
    };
    component=createWorkflowUI({state,escape,icon,badge:value=>`<span class="badge ${escape(value)}">${escape(({ACTIVE:"使用中",ARCHIVED:"已归档",DELETED:"已删除",QUEUED:"已接受",RUNNING:"执行中",WAITING:"等待",SUCCEEDED:"完成",FAILED:"失败",CANCELLED:"已取消",UNCERTAIN:"待核对",SKIPPED:"未执行"})[value]||value)}</span>`,markdown:renderMarkdown,rpc,toast:notify,render:draw,idempotency:()=>crypto.randomUUID(),ensureSession:async id=>{app.state.session=app.state.sessions.find(value=>value.id===id)||null;await app.reloadInstances();sync();}});
    entry={component,sync,draw};controllers.set(app,entry);
    app.onWorkflowEvent=fact=>{sync();component.onEvent({type:fact.kind,payload:{...fact.data,project_id:fact.project_id}}).catch(error=>notify(error.message,true));};
  }
  entry.sync();entry.draw();await entry.component.load(app.state.project?.id);
}
