import { test } from 'node:test';
import assert from 'node:assert/strict';
import { autoLayout, canConnect, fitViewport, portPoint } from '../src/oea/web/static/workflow-graph.mjs';

const graph = {nodes: [
  {id:'start',type:'start',position:{x:0,y:0}},
  {id:'a',type:'condition',position:{x:0,y:0}},
  {id:'b',type:'template',position:{x:0,y:0}},
  {id:'c',type:'template',position:{x:0,y:0}},
  {id:'end',type:'end',position:{x:0,y:0}},
],edges:[{id:'1',source:'start',target:'a'}, {id:'2',source:'a',target:'b',branch:'true'},
  {id:'3',source:'a',target:'c',branch:'false'}, {id:'4',source:'b',target:'end'}, {id:'5',source:'c',target:'end'}]};

test('connections allow native graph loops and reject duplicate ports and start/end violations', () => {
  assert.equal(canConnect(graph,'b','a'),'');
  assert.ok(canConnect(graph,'a','b','true'));
  assert.ok(canConnect(graph,'end','b'));
  assert.ok(canConnect(graph,'b','start'));
  assert.ok(canConnect(graph,'a','end','default'));
  assert.equal(canConnect(graph,'a','end','false'),'');
});

test('layout preserves the graph and separates parallel branches and joins', () => {
  const result = autoLayout(graph), get = id => result.nodes.find(node=>node.id===id);
  assert.equal(get('b').position.x,get('c').position.x);
  assert.notEqual(get('b').position.y,get('c').position.y);
  assert.ok(get('end').position.x > get('b').position.x);
  assert.equal(graph.nodes[1].position.x,0);
  assert.deepEqual(result.edges,graph.edges);
  assert.notDeepEqual(portPoint(get('a'),'true'),portPoint(get('a'),'false'));
});

test('fit places all nodes within the visible canvas at different sizes', () => {
  const arranged = autoLayout(graph);
  for (const [width,height] of [[1000,600],[400,400]]) {
    const view = fitViewport(arranged,width,height);
    assert.ok(view.zoom>=0.2 && view.zoom<=1);
    for (const node of arranged.nodes) {
      assert.ok(node.position.x*view.zoom+view.x>=0);
      assert.ok((node.position.x+216)*view.zoom+view.x<=width);
      assert.ok(node.position.y*view.zoom+view.y>=0);
    }
  }
});
