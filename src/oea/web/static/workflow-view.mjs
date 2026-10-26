// Browser presentation of canonical workflow records. All commands use v2 operations.
import { defaults } from "./configuration-form.mjs";

const states={active:"ACTIVE",archived:"ARCHIVED",deleted:"DELETED",accepted:"QUEUED",running:"RUNNING",waiting:"WAITING",completed:"SUCCEEDED",failed:"FAILED",cancelled:"CANCELLED",needs_review:"UNCERTAIN",skipped:"SKIPPED"};
export const displayState=value=>states[value]||value;
export const definitionView=value=>({...value,state:displayState(value.state)});
export const runView=value=>({...value,state:displayState(value.state),nodes:value.nodes?.map(node=>({...node,state:displayState(node.state),title:value.graph?.nodes.find(item=>item.id===node.node_id)?.title||node.node_id}))});
export const pageView=(page,view)=>({...page,items:page.items.map(view),has_more:page.cursor!==null});
export const nodeTypesView=types=>Object.entries(types).map(([type,{config_schema}])=>({type,config_schema,defaults:defaults(config_schema,config_schema)}));
export function orderedRuns(runs) {
  const active=new Set(["QUEUED","RUNNING","WAITING","UNCERTAIN"]);
  return [...runs].sort((left,right)=>Number(active.has(right.state))-Number(active.has(left.state)) || right.created_at-left.created_at);
}
