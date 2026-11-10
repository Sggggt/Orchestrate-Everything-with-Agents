// Agent declarations remain the source of truth. These functions only edit geometry and declarations.
export const WIDTH=224, HEIGHT=112;
export const copy=value=>structuredClone(value);
export const nodeKinds={
  prepare:["准备输入","file","执行","装配任务、权限与上下文"],
  model:["模型决策","spark","执行","根据指令选择工具、等待或交付"],
  tools:["执行工具","terminal","执行","执行模型选择的获准工具"],
  observe:["观察结果","eye","执行","读取工具结果并继续或等待"],
  recover_context:["恢复上下文","refresh","执行","沿原调用恢复上下文窗口"],
  deliver:["交付结果","check","执行","保存完整结果并结束执行"],
  set:["设置状态","sliders","数据","写入声明的状态字段"],
  condition:["条件分支","branch","控制","判断状态并选择真或假分支"],
  function:["Python 函数","code","数据","调用宿主已注册的纯函数"],
  remember:["保存记忆","book","数据","把模型输出保存为上下文引用"],
  dispatch:["并行分发","flow","控制","对数组执行独立纯计算并汇合"],
  subgraph:["子图","flow","控制","进入独立状态与映射的嵌套图"],
  wait:["等待输入","clock","控制","持久挂起，收到输入后沿原运行继续"]
};
export const branchLabels={default:"下一步",true:"是",false:"否",tools:"工具",deliver:"交付",wait:"等待",recover_context:"恢复",model:"继续"};
export function portsFor(node,graph){
  if(node.type==="deliver")return [];
  if(node.type==="condition")return ["true","false"];
  if(["model","observe"].includes(node.type)){
    const edges=graph.edges.filter(edge=>edge.source===node.id);
    if(edges.length===1&&edges[0].branch==="default")return ["default"];
    return node.type==="model"?["tools","deliver","wait","recover_context"]:["model","wait"];
  }
  return ["default"];
}
export function blankGraph(){return {format_version:1,nodes:[],edges:[],entry:"",state:{},max_steps:512,source:{},metadata:{}};}
export function layoutGraph(graph){
  const result=copy(graph),level=new Map(),queue=[];
  if(graph.nodes.some(node=>node.id===graph.entry)){level.set(graph.entry,0);queue.push(graph.entry);}
  while(queue.length){const id=queue.shift();for(const edge of graph.edges.filter(edge=>edge.source===id))if(!level.has(edge.target)){level.set(edge.target,level.get(id)+1);queue.push(edge.target);}}
  const columns=new Map();
  for(const node of result.nodes){const col=level.get(node.id)??0,row=columns.get(col)||0;node.position={x:64+col*320,y:64+row*180};columns.set(col,row+1);}
  return result;
}
export function ensureLayout(graph){
  const result=copy(graph);result.edges??=[];result.state??={};result.metadata??={};
  for(const node of result.nodes)node.position={x:Number.isFinite(node.position?.x)?node.position.x:0,y:Number.isFinite(node.position?.y)?node.position.y:0};
  if(result.nodes.length>1&&result.nodes.every(node=>!node.position||(!node.position.x&&!node.position.y)))return layoutGraph(result);
  return result;
}
export function bounds(graph){
  if(!graph.nodes.length)return {x:0,y:0,width:640,height:360};
  const xs=graph.nodes.map(n=>n.position.x),ys=graph.nodes.map(n=>n.position.y);
  return {x:Math.min(...xs)-40,y:Math.min(...ys)-40,width:Math.max(...xs)-Math.min(...xs)+WIDTH+80,height:Math.max(...ys)-Math.min(...ys)+HEIGHT+80};
}
export function fit(graph,width,height){const b=bounds(graph),zoom=Math.max(.08,Math.min(1.2,width/b.width,height/b.height));return {x:(width-b.width*zoom)/2-b.x*zoom,y:(height-b.height*zoom)/2-b.y*zoom,zoom};}
export function port(node,branch,graph,input=false){const ports=portsFor(node,graph),index=Math.max(0,ports.indexOf(branch));return {x:node.position.x+(input?0:WIDTH),y:node.position.y+(input?HEIGHT/2:30+(index+1)*65/(ports.length+1))};}
function curveGeometry(from,to){
  const approach={x:to.x-14,y:to.y};
  if(to.x>from.x+20){const bend=Math.max(48,(approach.x-from.x)/2);return {prefix:`M${from.x},${from.y}`,points:[from,{x:from.x+bend,y:from.y},{x:approach.x-bend,y:approach.y},approach],end:to};}
  const top=Math.min(from.y,to.y)-90;
  return {prefix:`M${from.x},${from.y} C${from.x+80},${from.y} ${from.x+80},${top} ${from.x},${top} L${approach.x},${top}`,points:[{x:approach.x,y:top},{x:approach.x-80,y:top},{x:approach.x-80,y:approach.y},approach],end:to};
}
const curvePath=curve=>`${curve.prefix} C${curve.points.slice(1).map(p=>`${p.x},${p.y}`).join(" ")} L${curve.end.x},${curve.end.y}`;
function curveArrow(curve){
  // The final straight approach is tangent to the curve and ends at the arrow tip.
  const tip=curve.end,previous=curve.points[3],dx=tip.x-previous.x,dy=tip.y-previous.y,length=Math.hypot(dx,dy);
  const x=dx/length,y=dy/length,base={x:tip.x-x*11,y:tip.y-y*11};
  return `M${base.x-y*4},${base.y+x*4} L${tip.x},${tip.y} L${base.x+y*4},${base.y-x*4} Z`;
}
export function pathBetween(from,to){return curvePath(curveGeometry(from,to));}
export function arrowBetween(from,to){return curveArrow(curveGeometry(from,to));}
export function edgeShape(edge,graph){const from=graph.nodes.find(n=>n.id===edge.source),to=graph.nodes.find(n=>n.id===edge.target);if(!from||!to)return {path:"",arrow:""};const input=port(to,"",graph,true),curve=curveGeometry(port(from,edge.branch,graph),{x:input.x-6,y:input.y});return {path:curvePath(curve),arrow:curveArrow(curve)};}
export function edgePath(edge,graph){return edgeShape(edge,graph).path;}
export function connect(graph,source,target,branch="default",id=crypto.randomUUID()){
  const from=graph.nodes.find(n=>n.id===source),to=graph.nodes.find(n=>n.id===target);
  if(!from||!to)throw new Error("连线节点不存在");
  if(!portsFor(from,graph).includes(branch))throw new Error("请选择正确的输出端口");
  if(graph.edges.some(e=>e.source===source&&e.target===target&&e.branch===branch))return graph;
  const result=copy(graph);
  if(["condition","model","observe"].includes(from.type))result.edges=result.edges.filter(e=>e.source!==source||e.branch!==branch);
  result.edges.push({id:"edge-"+id,source,target,branch});return result;
}
export function duplicate(graph,ids,offset={x:48,y:48}){
  const result=copy(graph),mapping=new Map(ids.map(id=>[id,"node-"+crypto.randomUUID()]));
  for(const node of graph.nodes.filter(node=>mapping.has(node.id))){const added=copy(node);added.id=mapping.get(node.id);added.position={x:node.position.x+offset.x,y:node.position.y+offset.y};if(added.type==="dispatch"&&mapping.has(added.config.target))added.config.target=mapping.get(added.config.target);result.nodes.push(added);}
  for(const edge of graph.edges.filter(e=>mapping.has(e.source)&&mapping.has(e.target)))result.edges.push({...edge,id:"edge-"+crypto.randomUUID(),source:mapping.get(edge.source),target:mapping.get(edge.target)});
  return {graph:result,ids:[...mapping.values()]};
}
export function deleteSelection(graph,ids,edgeId=null){const result=copy(graph),selected=new Set(ids);result.nodes=result.nodes.filter(n=>!selected.has(n.id));result.edges=result.edges.filter(e=>e.id!==edgeId&&!selected.has(e.source)&&!selected.has(e.target));if(selected.has(result.entry))result.entry=result.nodes[0]?.id||"";return result;}
export function thumbnail(value,escape){
  const graph=ensureLayout(value||blankGraph()),b=bounds(graph);
  return `<svg class="ag-thumbnail" viewBox="${b.x} ${b.y} ${b.width} ${b.height}" role="img" aria-label="Agent 节点画布预览"><g class="ag-links">${graph.edges.map(edge=>{const shape=edgeShape(edge,graph);return `<path class="ag-edge-line" d="${shape.path}"/><path class="ag-edge-arrow" d="${shape.arrow}"/>`;}).join("")}</g>${graph.nodes.map(node=>`<g transform="translate(${node.position.x} ${node.position.y})"><rect width="${WIDTH}" height="${HEIGHT}" rx="12" class="ag-mini-node"/><rect width="5" height="${HEIGHT-24}" x="0" y="12" rx="2" class="ag-mini-accent"/><text x="18" y="43">${escape((node.title||nodeKinds[node.type]?.[0]||node.id).slice(0,18))}</text><text x="18" y="74" class="ag-mini-type">${escape(nodeKinds[node.type]?.[0]||"Python 节点")}</text></g>`).join("")}</svg>`;
}
