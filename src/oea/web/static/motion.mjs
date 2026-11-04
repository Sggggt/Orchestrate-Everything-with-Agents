const easing="cubic-bezier(.2,.8,.2,1)";
export const reducedMotion=()=>globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches===true;
export function animate(node,keyframes,{duration=220,...options}={}){
  if(reducedMotion()||!node?.animate)return null;
  return node.animate(keyframes,{duration,easing,...options});
}
export function reveal(node){return animate(node,[{opacity:0,transform:"translateY(6px)"},{opacity:1,transform:"translateY(0)"}],{duration:200});}

let current=null;
export async function transitionView(update){
  if(!document.startViewTransition||reducedMotion()||document.visibilityState==="hidden"||document.querySelector(".v2-dialog-backdrop")){
    await update();reveal(document.querySelector("#page"));return;
  }
  current?.skipTransition();
  const transition=document.startViewTransition(update);current=transition;
  // A rapid second navigation can skip the visual transition. The update's
  // own rejection remains visible to its caller.
  transition.ready.catch(()=>{});
  transition.finished.catch(()=>{}).finally(()=>{if(current===transition)current=null;});
  await transition.updateCallbackDone;
}
