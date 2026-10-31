import { markdown } from "./markdown.mjs";

// Blank lines outside fences close blocks in the shared Markdown grammar.
export class MarkdownBlocks{
  constructor(){this.text="";this.start=0;this.scan=0;this.fence=false;this.count=0;}
  update(text){
    const reset=!text.startsWith(this.text),blocks=[];
    if(reset){this.start=0;this.scan=0;this.fence=false;this.count=0;}
    this.text=text;
    let end;
    while((end=text.indexOf("\n",this.scan))>=0){
      const line=text.slice(this.scan,end).replace(/\r$/,"");
      if(this.fence){if(/^\s*```\s*$/.test(line))this.fence=false;}
      else if(/^\s*```[\w+-]*\s*$/.test(line))this.fence=true;
      else if(!line.trim()){
        const source=text.slice(this.start,end+1);
        if(source.trim())blocks.push({id:this.count++,html:markdown(source)});
        this.start=end+1;
      }
      this.scan=end+1;
    }
    return {reset,blocks,tail:text.slice(this.start),tailId:this.count};
  }
}

export function streamMarkdown(root){
  let projection=new MarkdownBlocks(),identity=null,tail=null,previous="";
  return (text,key)=>{
    if(identity!==key){identity=key;projection=new MarkdownBlocks();root.replaceChildren();tail=null;previous="";}
    const value=projection.update(text);
    if(value.reset){root.replaceChildren();tail=null;previous="";}
    if(!tail){tail=root.ownerDocument.createElement("div");root.append(tail);}
    for(const block of value.blocks){
      const template=root.ownerDocument.createElement("template");template.innerHTML=block.html;
      for(const node of template.content.children)node.dataset.blockId=String(block.id);
      root.insertBefore(template.content,tail);
    }
    if(value.tail!==previous){tail.dataset.blockId=String(value.tailId);tail.innerHTML=markdown(value.tail);previous=value.tail;}
  };
}
