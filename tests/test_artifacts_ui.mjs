import test from "node:test";
import assert from "node:assert/strict";
import { artifactDownload,artifactSource,groupArtifacts,decodeArtifactText,readArtifactPreview } from "../src/oea/web/static/artifacts.mjs";

test("artifact origins group by stable Agent identity and preserve non-execution sources",()=>{
  const from=(id,agent_id,agent_name)=>({id,origin:"run",agent_id,agent_name});
  const items=[from("one","writer","Before rename"),from("two","reviewer","Reviewer"),from("three","writer","Before rename"),
    {id:"uploaded",origin:"upload"},{id:"imported",origin:"import"},{id:"unknown"}];
  const groups=groupArtifacts(items,[{id:"writer",name:"Same name"},{id:"reviewer",name:"Same name"}]);
  assert.deepEqual(groups.map(group=>group.source.key),["agent:writer","agent:reviewer","upload","import","unknown"]);
  assert.equal(groups[0].source.label,"Same name");
  assert.deepEqual(groups[0].items.map(item=>item.id),["one","three"]);
  assert.equal(groups[2].source.label,"用户上传");
  assert.equal(artifactSource(from("past","unregistered","Recorded name")).label,"Recorded name");
});

test("original read-only previews retain markup and Unicode rather than rendering it",async()=>{
  const text='# Original\r\n\n<script>example()</script>\n  **source**\tvalue';
  const bytes=new TextEncoder().encode(text);
  const preview=await readArtifactPreview({id:"example/id",project_id:"project space",bytes:bytes.length,media_type:"text/markdown"},{
    fetcher:async url=>{assert.equal(url,"/api/artifacts/example%2Fid/content?project_id=project%20space");return new Response(bytes);}
  });
  assert.deepEqual(preview,{kind:"text",text,encoding:"utf-8",truncated:false});
  assert.equal(decodeArtifactText(new Uint8Array()).text,"");
  assert.equal(decodeArtifactText(new Uint8Array([0xff,0xfe,0x9f,0x53,0x87,0x65])).text,"原文");
  assert.equal(decodeArtifactText(new Uint8Array([0xfe,0xff,0x53,0x9f,0x65,0x87])).text,"原文");
});

test("bounded preview cancels its stream without splitting a Unicode character",async()=>{
  const bytes=new TextEncoder().encode("ab你好cd");let cancelled=false;
  const body=new ReadableStream({start(controller){controller.enqueue(bytes);},cancel(){cancelled=true;}});
  const result=await readArtifactPreview({id:"bounded",project_id:"p",bytes:bytes.length},{limit:6,fetcher:async()=>new Response(body)});
  assert.equal(result.text,"ab你");assert.equal(result.truncated,true);assert.equal(cancelled,true);
});

test("unsupported binary files use the download and actual read errors remain errors",async()=>{
  let calls=0;
  const value=await readArtifactPreview({id:"image",project_id:"p",media_type:"image/png"},{fetcher:async()=>{calls++;}});
  assert.equal(value.kind,"unsupported");assert.equal(calls,0);
  assert.equal(decodeArtifactText(new Uint8Array([0,1,2])).kind,"unsupported");
  await assert.rejects(readArtifactPreview({id:"missing",project_id:"p"},{fetcher:async()=>new Response(JSON.stringify({error:{safe_message:"Original unavailable"}}),{status:404,headers:{"Content-Type":"application/json"}})}),/Original unavailable/);
  assert.equal(artifactDownload("id","p"),"/api/artifacts/id/content?project_id=p");
});
