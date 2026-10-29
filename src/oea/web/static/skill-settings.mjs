// Original directory/page component with v2 service bindings.
import { $,escape,icon,rpc,notify,confirmRemoval } from "./ui.mjs";
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
    const showRemove=(skill,after)=>confirmRemoval("删除 Skill",`删除 ${skill.name} 后将撤销后续读取。历史和源文件保留；关联的 Agent 需要重新配置该引用。`,after);
    component=createSkillIntegration({state,escape,icon,markdown,rpc,toast:notify,render:draw,loadBootstrap:load,showRemove});
    const refresh=async fact=>{if(!current())return false;if(fact.kind==="skill.removed"&&state.skillDetail?.id===fact.data.skill_id){state.skillEpoch++;state.skillDetail=null;}await load();draw();return true;};
    entry={load,draw,refresh};projects.set(project.id,entry);
  }
  app.onSkillEvent=fact=>entry.refresh(fact);
  await entry.load();entry.draw();
}
