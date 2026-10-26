// Original directory/page component with v2 service bindings.
import { $,escape,icon,rpc,notify } from "./ui.mjs";
import { markdown } from "./markdown.mjs";
import { createSkillIntegration } from "./skill-directory.mjs";

const controllers=new WeakMap();
export async function manageSkills(app,root){
  let projects=controllers.get(app);if(!projects){projects=new Map();controllers.set(app,projects);}
  const project=app.state.project;
  let entry=projects.get(project.id);
  if(!entry){
    const state={project,skillRegistry:{items:[]},skillDetail:null,skillDirectory:"",skillQuery:"",skillEpoch:0,skillFiles:[]};
    const current=()=>app.state.project?.id===project.id&&app.state.page==="skills";
    let component;
    const draw=()=>{
      const target=$("#page");if(!current()||!target)return;
      const focus=document.activeElement?.id,selection=document.activeElement?.selectionStart;
      target.className="";target.innerHTML=component.page();
      target.onclick=async event=>{const button=event.target.closest("[data-action]");if(!button||button.disabled)return;try{await component.action(button.dataset.action,button);}catch(error){notify(error.message,true);}};
      target.oninput=event=>component.input(event.target);
      target.onsubmit=async event=>{event.preventDefault();try{await component.submit(event);}catch(error){notify(error.message,true);}};
      if(focus){const input=target.querySelector('[id="'+CSS.escape(focus)+'"]');if(input){input.focus({preventScroll:true});if(selection!=null&&input.type==="search")input.setSelectionRange(selection,selection);}}
    };
    const load=async()=>{const page=await rpc("skills.list",{project_id:project.id,limit:25});state.skillRegistry={items:page.items.map(row=>({...row.definition,directory:row.definition.path,revision:String(row.revision),bound_agents:row.bound_agents||[]})),cursor:page.cursor,has_more:page.cursor!=null};if(app.state.project?.id===project.id){app.state.skills=page.items;app.refreshFrame();}};
    component=createSkillIntegration({state,escape,icon,markdown,rpc,toast:notify,render:draw,loadBootstrap:load});
    entry={load,draw};projects.set(project.id,entry);
  }
  await entry.load();entry.draw();
}
