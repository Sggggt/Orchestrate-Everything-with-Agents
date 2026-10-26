// Rendering follows the server's configuration schema; labels are presentation.
export const labels = {
  id:"标识",name:"名称",description:"职责说明",enabled:"启用",model:"模型",base_url:"API 地址",credential_ref:"密钥来源引用",
  system:"职责与系统指令",agent_md:"Agent 运行指令",temperature:"温度",reasoning_effort:"推理强度",max_output_tokens:"单次最大输出 token",
  context_window_tokens:"模型上下文窗口",token_parameter:"供应商输出参数",timeout_seconds:"请求超时（秒）",local:"本地工具",functions:"Python 函数工具",
  mcp:"MCP 服务绑定",server_id:"服务标识",tools:"允许的工具",skills:"Skills",path:"目录",required:"执行前必需",input_tokens:"工作输入 token",
  pressure:"记忆更新压力比例",tool_inline_tokens:"工具结果内联 token",memory_tokens:"阶段记忆 token",safety_tokens:"安全余量 token",recent_turns:"近期保留消息组",
  project:"项目原件目录",work:"实例工作目录的根目录",references:"只读参考目录",writable_project_paths:"允许写入的项目子目录",shell:"Shell 参数",environment:"环境变量",
  command_timeout_seconds:"命令超时（秒）",graph:"执行图",graph_version:"代码图版本",program:"进程程序与参数",program_uses_model:"程序需要模型",budget:"费用与时间预算",
  delivery:"交付要求",text_required:"要求正文",min_artifacts:"最少产物数",output_schema:"结构化结果 JSON Schema",max_model_calls:"最大模型调用次数",
  max_input_tokens:"累计输入 token 上限",max_output_tokens_budget:"累计输出 token 上限",max_tool_calls:"最大工具调用次数",max_elapsed_seconds:"最长工作时间（秒）",
  max_stalled_model_calls:"无新证据的模型调用上限",departments:"获准伙伴 Agent标识",can_request:"允许向伙伴请求工作",communication:"通信权限",manager:"manager",
  instructions:"运行指令",context:"上下文与记忆",workspace:"工作环境",execution:"执行与交付",max_steps:"图步数上限（可选）",
  transport:"连接方式",command:"程序与参数",cwd:"程序启动目录",url:"服务地址",environment_refs:"环境变量秘密引用",header_refs:"HTTP 头秘密引用",program_timeout_seconds:"单次进程执行超时（秒）",revision:"Skill 修订（留空采用最新）"
  ,outputs:"业务输出",input:"工作输入",agent_id:"Agent 能力标识",items:"映射列表",outcome:"结果策略",left:"左值",operator:"比较方式",right:"右值",value:"值",arguments:"函数参数",workflow_id:"子图标识",inputs:"子图输入",prompt:"等待提示"
};
const copy = value => structuredClone(value);
export function resolveSchema(schema, root) {
  let value = schema;
  if (value.$ref) value = {...root.$defs[value.$ref.split("/").at(-1)], ...value};
  if (value.anyOf) value = {...value.anyOf.find(item => item.type !== "null"), ...Object.fromEntries(Object.entries(value).filter(([key]) => key !== "anyOf")), nullable:value.anyOf.some(item=>item.type==="null")};
  if (value.$ref) value = {...root.$defs[value.$ref.split("/").at(-1)], ...value};
  return value;
}
export function defaults(schema, root) {
  const value = resolveSchema(schema, root);
  if ("default" in value) return copy(value.default);
  if (value.nullable) return null;
  if (value.type === "object") return Object.fromEntries(Object.entries(value.properties || {}).map(([key, item])=>[key, defaults(item,root)]));
  if (value.type === "array") return [];
  if (value.type === "boolean") return false;
  if (value.type === "integer" || value.type === "number") return value.minimum ?? 0;
  return value.enum?.[0] ?? value.const ?? "";
}
export function schemaAt(root, path) {
  return path.reduce((schema,key)=>resolveSchema(schema,root).properties[key],root);
}
export function getAt(object,path) { return path.reduce((value,key)=>value?.[key],object); }
export function setAt(object,path,value) {
  const keys=[...path], last=keys.pop();
  let owner=object;
  for (const key of keys) { owner[key] ??= {}; owner=owner[key]; }
  owner[last]=value;
}
export function formField({schema,root,get,set,name,path=[],changed=()=>{},basic=null}) {
  const spec=resolveSchema(schema,root), box=document.createElement("div"), title=(name==="max_output_tokens" && path.includes("budget")?labels.max_output_tokens_budget:labels[name])||(/[^\w]/.test(name)?name:spec.title||name);
  box.className="field schema-field";
  const label=document.createElement("label"); label.textContent=title;
  const identity="field-"+crypto.randomUUID(); label.htmlFor=identity; box.append(label);
  let value=get();
  if (value===undefined) value=defaults(schema,root);
  const update=value=>{set(value); changed();};
  if (spec.type==="object" && spec.properties) {
    box.className="schema-object";
    const heading=document.createElement("h3"); heading.textContent=title; box.replaceChildren(heading);
    if (spec.nullable) {
      const toggle=document.createElement("input"); toggle.type="checkbox"; toggle.checked=value!==null;
      const caption=document.createElement("label"); caption.className="toggle-row"; caption.append(toggle,document.createTextNode("启用独立配置")); box.append(caption);
      toggle.addEventListener("change",()=>{const enabled=Object.fromEntries(Object.entries(spec).filter(([key])=>key!=="default")); update(toggle.checked?defaults({...enabled,nullable:false},root):null); box.replaceWith(formField({schema,root,get,set,name,path,changed,basic}));});
      if (value===null) return box;
    }
    const grid=document.createElement("div"); grid.className="form-grid";
    const advanced=document.createElement("details"); advanced.className="schema-advanced"; advanced.innerHTML="<summary>高级参数</summary>";
    const extra=document.createElement("div"); extra.className="form-grid"; advanced.append(extra);
    for (const [key, child] of Object.entries(spec.properties)) {
      const field=formField({schema:child,root,get:()=>get()?.[key],set:item=>{const current=copy(get()||{}); current[key]=item; set(current);},name:key,path:[...path,key],changed,basic:key==="model"?["base_url","model","credential_ref"]:null});
      const target=basic && !basic.includes(key)?extra:grid; target.append(field);
    }
    box.append(grid); if (extra.children.length) box.append(advanced); return box;
  }
  if (spec.type==="array") {
    const item=resolveSchema(spec.items||{},root);
    if (item.enum) {
      const options=document.createElement("div"); options.className="check-options";
      for (const choice of item.enum) {
        const input=document.createElement("input"); input.type="checkbox"; input.checked=(value||[]).includes(choice);
        const caption=document.createElement("label"); caption.append(input,document.createTextNode(choice)); options.append(caption);
        input.addEventListener("change",()=>{const selected=new Set(get()||[]); input.checked?selected.add(choice):selected.delete(choice); update([...selected]);});
      }
      box.append(options); return box;
    }
    if (item.type==="object" && item.properties) {
      const entries=document.createElement("div"); entries.className="schema-entries";
      (value||[]).forEach((entry,index)=>{
        const card=formField({schema:spec.items,root,get:()=>get()?.[index],set:item=>{const next=copy(get()||[]); next[index]=item; set(next);},name:labels[name]+" "+(index+1),path:[...path,index],changed});
        const remove=document.createElement("button"); remove.type="button"; remove.className="btn btn-small"; remove.textContent="移除";
        remove.addEventListener("click",()=>{update((get()||[]).filter((_,i)=>i!==index)); box.replaceWith(formField({schema,root,get,set,name,path,changed}));}); card.append(remove); entries.append(card);
      });
      const add=document.createElement("button"); add.type="button"; add.className="btn btn-small"; add.textContent="添加 "+title;
      add.addEventListener("click",()=>{update([...(get()||[]),defaults(spec.items,root)]); box.replaceWith(formField({schema,root,get,set,name,path,changed}));});
      box.append(entries,add); return box;
    }
  }
  let input;
  if (spec.enum || spec.const!==undefined) {
    input=document.createElement("select");
    for (const choice of [...(spec.nullable?[null]:[]),...(spec.enum||[spec.const])]) {const option=document.createElement("option"); option.value=choice??""; option.textContent=choice??"未设置"; input.append(option);}
    input.value=value??""; input.addEventListener("change",()=>update(input.value||null));
  } else if (spec.type==="boolean") {
    input=document.createElement("input"); input.type="checkbox"; input.checked=!!value; input.addEventListener("change",()=>update(input.checked));
  } else if (spec.type==="integer" || spec.type==="number") {
    input=document.createElement("input"); input.type="number"; input.step=spec.type==="integer"?"1":"any"; input.value=value??"";
    if (spec.minimum!==undefined) input.min=spec.minimum; if (spec.maximum!==undefined) input.max=spec.maximum;
    input.addEventListener("input",()=>{if(input.value!=="" && Number.isFinite(Number(input.value))) update(Number(input.value)); else if(spec.nullable)update(null);});
  } else {
    const array=spec.type==="array", json=spec.type==="object" || (!spec.type && !spec.enum);
    const long=array||json||["system","agent_md","description"].includes(name);
    input=document.createElement(long?"textarea":"input");
    if (!long) input.type="text";
    input.value=json?(value==null?"":JSON.stringify(value,null,2)):array?(value||[]).join("\n"):value??"";
    if (long) input.rows=json?7:array?3:5;
    if (spec.maxLength) input.maxLength=spec.maxLength;
    input.addEventListener("input",()=>{
      try {const next=json?(input.value.trim()?JSON.parse(input.value):null):array?input.value.split("\n").map(item=>item.trim()).filter(Boolean):input.value|| (spec.nullable?null:""); input.setCustomValidity(""); update(next);}
      catch {input.setCustomValidity("请输入有效的 JSON");}
    });
    if (array) {const hint=document.createElement("small"); hint.textContent="每行一项"; box.append(hint);}
    if (name==="credential_ref") input.placeholder="env:MODEL_API_KEY 或已保存的 vault: 引用";
  }
  input.id=identity;
  if (spec.description) {const hint=document.createElement("small"); hint.textContent=spec.description; box.append(hint);}
  box.append(input); return box;
}
