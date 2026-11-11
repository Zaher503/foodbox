// Shared by the app (index.html) and the public share page (share.html): building elements, quantities,
// scaling, metric conversion, ingredient splitting, times, and cook mode. Loaded before each page's own script.
/* eslint-disable no-unused-vars */
const $=s=>document.querySelector(s);
const el=(t,p={},...k)=>{const e=document.createElement(t);for(const[a,v]of Object.entries(p)){if(a==="class")e.className=v;else if(a.startsWith("on"))e[a]=v;else if(v!==false&&v!=null)e.setAttribute(a,v===true?"":v)}for(const c of k.flat())if(c!=null)e.append(c);return e};

const store={get:k=>{try{return localStorage.getItem(k)}catch{return null}},set:(k,v)=>{try{localStorage.setItem(k,v)}catch{}}};

const NUT_LABELS=["Serving size","Calories","Fat","Saturated fat","Carbohydrates","Fiber","Sugar","Protein","Cholesterol","Sodium"];
const FC="½¼¾⅓⅔⅛⅜⅝⅞",FR={"½":.5,"¼":.25,"¾":.75,"⅓":1/3,"⅔":2/3,"⅛":.125,"⅜":.375,"⅝":.625,"⅞":.875};
const NUM=`(?:\\d+\\s+\\d+\\s*[/⁄]\\s*\\d+|\\d+\\s*[/⁄]\\s*\\d+|\\d+\\s*[${FC}]|[${FC}]|\\d+(?:\\.\\d+)?)`;
const QTY=new RegExp(`^(\\s*)(${NUM})(?:(\\s*(?:-|–|to)\\s*)(${NUM}))?(?![\\d/⁄-])`);
function toNum(s){
  s=s.trim();let m;
  if(m=s.match(/^(\d+)\s+(\d+)\s*[\/⁄]\s*(\d+)$/))return +m[1]+m[2]/m[3];
  if(m=s.match(/^(\d+)\s*[\/⁄]\s*(\d+)$/))return m[1]/m[2];
  if(m=s.match(new RegExp(`^(\\d*)\\s*([${FC}])$`)))return(+m[1]||0)+FR[m[2]];
  return parseFloat(s);
}
function fmt(v){
  const w=Math.floor(v+1e-9),fr=v-w;
  const T=[[0,""],[1/8,"1/8"],[1/4,"1/4"],[1/3,"1/3"],[3/8,"3/8"],[1/2,"1/2"],[5/8,"5/8"],[2/3,"2/3"],[3/4,"3/4"],[7/8,"7/8"],[1,""]];
  let best=T[0],bd=9;for(const t of T){const d=Math.abs(fr-t[0]);if(d<bd){bd=d;best=t}}
  if(bd<0.03){const W=w+(best[0]===1?1:0);return best[1]?(W?W+" ":"")+best[1]:String(W)}
  return String(Math.round(v*100)/100);
}
// scale the leading quantity of a line ("1 1/2 cups flour", "2-3 tbsp oil"); amounts in parentheses are left alone
function scaleText(s,m){
  if(m===1)return[s];
  const x=QTY.exec(s);if(!x)return[s];
  const a=toNum(x[2]);if(!isFinite(a))return[s];
  let out=fmt(a*m);
  if(x[4]){const b=toNum(x[4]);if(!isFinite(b))return[s];out+=x[3]+fmt(b*m)}
  return[x[1],el("b",{class:"q"},out),s.slice(x[0].length)];
}

// links to other recipes live in step text as [[Title|recipe-id]]; the editor shows [[Title]] and maps it back on save
const RL=/\[\[([^\]|]+)\|([\w.\-]+)\]\]/g;

const plainLinks=t=>t.replace(RL,"$1");

const UNITS=[[/^fl\.?\s*oz\b\.?/i,29.5735,"ml"],[/^cups?\b/i,240,"ml"],[/^pints?\b/i,473,"ml"],[/^(tablespoons?|tbsps?|tbs)\b\.?/i,15,"ml"],
  [/^(teaspoons?|tsps?)\b\.?/i,5,"ml"],[/^(ounces?|oz)\b\.?/i,28.3495,"g"],[/^(pounds?|lbs?)\b\.?/i,453.592,"g"]];
function unitAt(rest){
  const sp=/^\s*/.exec(rest)[0].length,t=rest.slice(sp);
  for(const[re,f,u]of UNITS){const x=re.exec(t);if(x)return{f,u,len:sp+x[0].length}}
  return null;
}
function roundM(v,u){return u==="g"?(v<100?Math.round(v):Math.round(v/5)*5):(v<10?Math.round(v*2)/2:v<100?Math.round(v):Math.round(v/5)*5)}
function metricText(vals,u,sep){
  const vs=vals.map(v=>roundM(v,u));
  if(Math.max(...vs)>=1000)return vs.map(v=>+(v/1000).toFixed(2)).join(sep)+(u==="g"?" kg":" L");
  return vs.join(sep)+" "+u;
}
// "(15 oz)" style can sizes -> "(425 g)"; never scaled
function parenMetric(s){
  return s.replace(/\((\d+(?:\.\d+)?)\s*(fl\.?\s*oz|ounces?|oz|pounds?|lbs?|cups?|pints?)\)/gi,(m,n,u)=>{const x=unitAt(u);return x?"("+metricText([n*x.f],x.u,"-")+")":m});
}
const tempText=(s,metric)=>metric?s.replace(/(\d{3})\s*°\s*F\b/g,(m,f)=>Math.round((f-32)*5/9/5)*5+"°C"):s;
// "240ml / 1 cup Chicken Stock", "4 x 120g / 4oz Salmon": two measures for one amount. Show one, never both.
const MET_U="kg|kilograms?|grams?|g|ml|millilit(?:er|re)s?|lit(?:er|re)s?|l",US_U="fl\\.?\\s*oz|cups?|tablespoons?|tbsps?|tbs|teaspoons?|tsps?|ounces?|oz|pounds?|lbs?|pints?|quarts?|qt";
const MEAS=`(${NUM}(?:\\s*(?:-|–|to)\\s*${NUM})?)\\s*(${MET_U}|${US_U})\\b\\.?`;
const DUAL=new RegExp(`^(\\s*(?:${NUM}\\s*[x×]\\s*)?)${MEAS}\\s*(?:/|⁄|\\bor\\b)\\s*${MEAS}(?=[\\s,;(]|$)`,"i");
const isMet=u=>new RegExp(`^(?:${MET_U})$`,"i").test(u);
function oneMeasure(s,metric){
  const x=DUAL.exec(s);if(!x)return s;
  const a={q:x[2],u:x[3]},b={q:x[4],u:x[5]};
  let pick=a;
  if(isMet(a.u)!==isMet(b.u))pick=(isMet(a.u)===!!metric)?a:b;
  return x[1]+pick.q+" "+pick.u+s.slice(x[0].length);
}
// one ingredient line: scale the leading quantity, optionally convert US units to metric
function lineNodes(s,m,metric){
  s=oneMeasure(s,metric);
  const x=QTY.exec(s),plain=()=>[metric?parenMetric(s):s];
  if(!x)return plain();
  const a=toNum(x[2]),b=x[4]?toNum(x[4]):null;
  if(!isFinite(a)||(b!==null&&!isFinite(b)))return plain();
  const rest=s.slice(x[0].length),u=metric?unitAt(rest):null;
  if(m===1&&!u)return plain();
  let txt,after=rest;
  if(u){txt=metricText(b===null?[a*m*u.f]:[a*m*u.f,b*m*u.f],u.u,x[3]||"-");after=rest.slice(u.len)}
  else txt=fmt(a*m)+(b!==null?x[3]+fmt(b*m):"");
  return[x[1],el("b",{class:"q"},txt),metric?parenMetric(after):after];
}
const AMT=/^[\d¼-¾⅐-⅞][\d¼-¾⅐-⅞\s.\/⁄–-]*(?:\s*[x×]\s*[\d¼-¾⅐-⅞][\d¼-¾⅐-⅞\s.\/⁄–-]*)?(?:\([^)]*\)\s*)?(?:(?:fl\.?\s*oz|cups?|tablespoons?|tbsps?|tbs|teaspoons?|tsps?|ounces?|oz|pounds?|lbs?|grams?|g|kilograms?|kg|milliliters?|ml|liters?|l|cloves?|cans?|pinch(?:es)?|dash(?:es)?|slices?|pieces?|packages?|pkgs?|sticks?|bunch(?:es)?|sprigs?|heads?|stalks?|quarts?|qt|pints?|pt|gallons?|jars?|bags?|handfuls?|cubes?)\b\.?)?/i;
// split an ingredient line into [amount nodes, name nodes]
function ingSplit(s,m,metric){
  const nodes=lineNodes(s,m,metric),full=nodes.map(n=>typeof n==="string"?n:n.textContent).join("");
  const x=AMT.exec(full);let n=x?x[0].replace(/\s+$/,"").length:0;
  const a=[],b=[];
  for(const nd of nodes){
    const t=typeof nd==="string"?nd:nd.textContent;
    if(n<=0){b.push(nd);continue}
    if(typeof nd!=="string"||t.length<=n){a.push(nd);n-=t.length;continue}
    a.push(t.slice(0,n));b.push(t.slice(n));n=0;
  }
  if(b.length&&typeof b[0]==="string")b[0]=b[0].replace(/^\s+/,"");
  return[a,b];
}
const fmtMin=n=>n>=60?Math.floor(n/60)+" hr"+(n%60?" "+n%60+" min":""):n+" min";
function timesLine(r){
  const b=[],t=r.total_min||(r.prep_min&&r.cook_min?r.prep_min+r.cook_min:0);
  if(r.prep_min)b.push(["Prep",fmtMin(r.prep_min)]);
  if(r.cook_min)b.push(["Cook",fmtMin(r.cook_min)]);
  if(r.active_min&&!r.cook_min)b.push(["Active",fmtMin(r.active_min)]);
  if(t)b.push(["Total",fmtMin(t)]);
  return b.length?el("div",{class:"times cards"},b.map(([k,v])=>el("div",{},el("span",{},k),el("b",{},v)))):null;
}

// cook mode: full-screen big steps, ingredients panel, screen stays on (Wake Lock)
let wl=null,cookEl=null;
async function keepAwake(){
  try{if("wakeLock"in navigator){if(!wl){wl=await navigator.wakeLock.request("screen");wl.addEventListener("release",()=>{wl=null})}return true}}catch{}
  return false;
}
function cook(r,mult,metric){
  const steps=[],heads=[];let curHd="";
  for(const s of (r.steps||[])){if(isHd(s)){curHd=hdText(s);continue}steps.push(plainLinks(tempText(s,metric)));heads.push(curHd)}
  if(!steps.length||cookEl)return;
  let i=0,fs=+store.get("ckfs")||28;
  const num=el("div",{class:"n"}),tx=el("div",{class:"tx"}),prog=el("i"),ttl=el("div",{class:"ttl"},r.title||""),aw=el("div",{class:"ck-aw"},"");
  const have=new Set();
  const ing=el("div",{class:"ck-ing"},el("h2",{style:"margin-top:0"},"Ingredients"),...(r.ingredients||[]).map(x=>{
    if(isHd(x))return el("div",{class:"ck-hd"},hdText(x));
    const sp=ingSplit(x,mult,metric),row=el("div",{class:"ck-i",onclick:()=>{have.has(x)?have.delete(x):have.add(x);row.classList.toggle("have",have.has(x))}},...sp[0]," ",...sp[1]);
    return row}));
  const prev=el("button",{class:"btn",onclick:()=>go(-1)},"Back"),next=el("button",{class:"btn pri",onclick:()=>go(1)},"Next →");
  const step=el("div",{class:"ck-step"},num,tx);
  const ingBtn=el("button",{class:"btn",id:"ckIngBtn",onclick:()=>ing.classList.toggle("show")},"Ingredients");
  const setFs=d=>{fs=Math.max(18,Math.min(56,fs+d));store.set("ckfs",fs);root.style.setProperty("--ckf",fs+"px")};
  const exit=()=>{removeEventListener("keydown",key);document.removeEventListener("visibilitychange",vis);cookEl.remove();cookEl=null;
    if(wl){wl.release().catch(()=>{});wl=null}};
  const root=el("div",{id:"cook"},
    el("div",{class:"ck-top"},el("button",{class:"btn",onclick:exit},"Exit"),ttl,
      el("button",{class:"btn","aria-label":"Smaller text",onclick:()=>setFs(-4)},"A−"),el("button",{class:"btn","aria-label":"Bigger text",onclick:()=>setFs(4)},"A+"),ingBtn),
    el("div",{class:"ck-prog"},prog),
    el("div",{class:"ck-body"},step,ing),aw,
    el("div",{class:"ck-nav"},prev,next));
  function go(d){
    if(i+d>=steps.length){exit();return}
    i=Math.max(0,i+d);ing.classList.remove("show");paint();step.scrollTop=0;
  }
  function paint(){
    num.textContent=(heads[i]?heads[i]+" \u00b7 ":"")+`Step ${i+1} of ${steps.length}`;tx.textContent=steps[i];
    prog.style.width=((i+1)/steps.length*100)+"%";
    prev.disabled=i===0;prev.style.opacity=i===0?".4":"";
    next.textContent=i===steps.length-1?"Done":"Next";
  }
  const key=e=>{
    if(e.target.closest("input,textarea"))return;
    if(e.key==="ArrowRight"||e.key==="PageDown"||e.key===" "){e.preventDefault();go(1)}
    else if(e.key==="ArrowLeft"||e.key==="PageUp"){e.preventDefault();go(-1)}
    else if(e.key==="Escape")exit();
  };
  const vis=()=>{if(!document.hidden)keepAwake().then(ok=>{aw.textContent=ok?"Screen stays on":"Screen may sleep (not supported here)"})};
  let x0=null;
  step.addEventListener("touchstart",e=>{x0=e.touches[0].clientX},{passive:true});
  step.addEventListener("touchend",e=>{if(x0===null)return;const dx=e.changedTouches[0].clientX-x0;x0=null;if(Math.abs(dx)>70)go(dx<0?1:-1)});
  addEventListener("keydown",key);document.addEventListener("visibilitychange",vis);
  cookEl=root;document.body.append(root);setFs(0);paint();vis();
}

const isHd=t=>/^##\s/.test(t),hdText=t=>t.replace(/^##\s*/,"");
