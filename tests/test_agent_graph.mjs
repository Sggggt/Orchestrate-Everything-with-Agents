import test from 'node:test';
import assert from 'node:assert/strict';
import {ensureLayout,layoutGraph,fit,portsFor,connect,duplicate,deleteSelection,thumbnail,edgePath,edgeShape} from '../src/oea/web/static/agent-graph.mjs';
import {architectureCall,acceptBinding} from '../src/oea/web/static/architecture-settings.mjs';

const graph=()=>({entry:'prepare',state:{counter:{type:'integer',reducer:'sum',default:0}},metadata:{advanced:{keep:true}},nodes:[
  {id:'prepare',type:'prepare',position:{x:0,y:0},config:{}},
  {id:'model',type:'model',position:{x:0,y:0},config:{instruction:'Keep instructions'},policy:{tools:{allowed:['exec_command']}}},
  {id:'deliver',type:'deliver',position:{x:0,y:0},config:{}}
],edges:[{id:'one',source:'prepare',target:'model',branch:'default'},{id:'two',source:'model',target:'deliver',branch:'default'}]});

test('layout and fit preserve executable fields and accommodate cycles',()=>{
  const original=graph();original.edges.push({id:'cycle',source:'model',target:'prepare',branch:'tools'});
  const placed=layoutGraph(original);
  assert.deepEqual(placed.metadata,original.metadata);assert.deepEqual(placed.state,original.state);assert.equal(original.nodes[1].position.x,0);
  assert.ok(placed.nodes[1].position.x>placed.nodes[0].position.x);assert.ok(fit(placed,800,600).zoom>0);
  assert.deepEqual(ensureLayout(placed),placed);
});
test('decision ports follow runtime branches and permit explicit single successors',()=>{
  const g=graph();assert.deepEqual(portsFor(g.nodes[1],g),['default']);g.edges.pop();
  assert.deepEqual(portsFor(g.nodes[1],g),['tools','deliver','wait','recover_context']);
  assert.deepEqual(portsFor({id:'observe',type:'observe'},g),['model','wait']);
  assert.throws(()=>connect(g,'deliver','model'));
  const connected=connect(g,'model','deliver','deliver','fixed');assert.equal(connected.edges.length,2);
  assert.equal(connect(connected,'model','deliver','deliver'),connected);
});
test('copy remaps internal edges while preserving advanced node settings',()=>{
  const g=ensureLayout(graph()),result=duplicate(g,['model','deliver']);
  assert.equal(result.graph.nodes.length,5);assert.equal(result.graph.edges.length,3);
  assert.deepEqual(result.graph.nodes[3].policy,g.nodes[1].policy);
  assert.equal(result.graph.edges.at(-1).source,result.ids[0]);
  assert.equal(result.graph.edges.at(-1).target,result.ids[1]);
  const removed=deleteSelection(result.graph,result.ids);assert.deepEqual(removed,g);
});
test('thumbnail exposes escaped text without executable node configuration',()=>{
  const g=ensureLayout(graph());g.nodes[1].title='<script>';
  const html=thumbnail(g,value=>String(value).replaceAll('<','&lt;').replaceAll('>','&gt;'));
  assert.ok(html.includes('&lt;script&gt;'));assert.ok(!html.includes('Keep instructions'));assert.ok(!html.includes('exec_command'));
});

test('incomplete draft coordinates cannot become SVG attributes',()=>{
  const g=graph();g.nodes[0].position={x:'"><image href="bad">',y:Infinity};
  const normalized=ensureLayout(g);
  assert.ok(normalized.nodes.every(n=>Number.isFinite(n.position.x)&&Number.isFinite(n.position.y)));
  assert.ok(!thumbnail(g,String).includes('<image'));
});

test('browser candidate creation sends project ownership inside the definition',async()=>{
  const calls=[],call=architectureCall({rpc:async(operation,params)=>{calls.push({operation,params});return {revision:1};}},'project');
  const definition={id:'candidate',project_id:'project',owner_agent_id:'agent',name:'Candidate'};
  await call('create',{definition,draft:graph()});
  assert.deepEqual(Object.keys(calls[0].params),['definition','draft']);
  assert.equal(calls[0].params.definition.project_id,'project');
  await call('save',{architecture_id:'candidate',expected_revision:1,draft:graph()});
  assert.equal(calls[1].params.project_id,'project');
});

test('binding preserves unsaved settings without adopting unseen concurrent revisions',()=>{
  const draft={revision:4,dirty:true,definition:{name:'Local change',execution:{budget:{max_model_calls:9},architecture:null}}};
  const saved={revision:5,definition:{name:'Server name',execution:{graph:'default',graph_version:'1',program:[],architecture:{architecture_id:'new',version:1},budget:{max_model_calls:2}}}};
  acceptBinding(draft,saved);
  assert.equal(draft.revision,5);assert.equal(draft.definition.name,'Local change');assert.equal(draft.definition.execution.budget.max_model_calls,9);
  acceptBinding(draft,{...saved,revision:7});
  assert.equal(draft.revision,5);assert.equal(draft.bindingConflict,true);assert.equal(draft.dirty,true);
});

test('arrows terminate each line at the target port boundary',()=>{
  const g=ensureLayout(graph()),before=structuredClone(g);
  for(const edge of [...g.edges,{source:'deliver',target:'prepare',branch:'default'}]){
    const target=g.nodes.find(node=>node.id===edge.target),shape=edgeShape(edge,g);
    const numbers=shape.arrow.match(/-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/g).map(Number),[ax,ay,tx,ty,bx,by]=numbers;
    assert.equal(tx,target.position.x-6);assert.equal(ty,target.position.y+56);
    assert.ok(shape.path.endsWith(`L${tx},${ty}`),'Arrow tip must be the end of the line');
    assert.ok(Math.abs(Math.hypot(ax-bx,ay-by)-8)<1e-6);
    assert.ok(Math.abs(Math.hypot(tx-(ax+bx)/2,ty-(ay+by)/2)-11)<1e-6);
  }
  assert.deepEqual(g,before);
});

test('curved and returning edges have a smooth final approach under the arrow',()=>{
  const g=graph();g.nodes[0].position={x:0,y:0};g.nodes[1].position={x:300,y:400};
  for(const edge of [g.edges[0],{source:'model',target:'prepare',branch:'default'}]){
    const shape=edgeShape(edge,g),lastC=shape.path.split(' C').at(-1).split(' L')[0].split(' ').map(p=>p.split(',').map(Number));
    const [ax,ay,tx,ty,bx,by]=shape.arrow.match(/-?\d+(?:\.\d+)?/g).map(Number);
    assert.equal(lastC[1][1],ty);assert.equal(lastC[2][1],ty);
    assert.equal(lastC[2][0],tx-14);
    assert.ok((ax+bx)/2>lastC[2][0]);assert.equal((ay+by)/2,ty);
  }
  const html=thumbnail(g,String);
  assert.equal((html.match(/class="ag-edge-arrow"/g)||[]).length,g.edges.length);
});
