// Public, read-only recipe page at /s/<token>. Nothing here writes to the server: the only requests are GETs.
// Scale, Metric and ticked-off ingredients are view settings kept in this browser only.
(async()=>{
  const token=location.pathname.split("/")[2]||"",root=document.getElementById("app");
  const mq=matchMedia("(min-width:860px)"),setDT=()=>document.documentElement.classList.toggle("dt",mq.matches);
  setDT();mq.addEventListener("change",setDT);
  const fail=(t,s)=>{root.className="";root.removeAttribute("aria-busy");root.replaceChildren(el("div",{class:"msg"},el("b",{},t),s))};
  let r;
  try{
    const res=await fetch(`/s/${encodeURIComponent(token)}/recipe`,{credentials:"omit",cache:"no-store"});
    if(res.status===404)return fail("This link doesn't work anymore","The person who shared it may have stopped sharing this recipe.");
    if(!res.ok)throw 0;
    r=await res.json();
  }catch{return fail("Couldn't load the recipe","Check your connection and reload the page.")}
  document.title=r.title||"Recipe";

  let mult=1,metric=store.get("metric")==="1";
  const host=u=>{try{return new URL(u).hostname.replace(/^www\./,"")}catch{return""}};
  const have=new Set();

  const hero=r.cover?el("img",{class:"hero",src:r.cover,alt:"",referrerpolicy:"no-referrer",onerror:e=>e.target.remove()}):null;
  const serv=el("span");
  const meta=el("div",{class:"meta"},
    r.meal&&r.meal!=="Other"?el("span",{class:"chip"},r.meal):null,serv,
    r.source_url?el("a",{href:r.source_url,target:"_blank",rel:"noopener noreferrer"},"Source: "+host(r.source_url)+" ↗")
      :r.source_note?el("span",{},r.source_note):null);
  const ingList=el("ul",{class:"ing"}),stepList=el("ol",{class:"st"});

  // scale: halves below 1, whole numbers above (same steps as the app)
  const out=el("output",{"aria-live":"polite"});
  const down=el("button",{type:"button","aria-label":"Make less",onclick:()=>bump(-1)},"−");
  const up=el("button",{type:"button","aria-label":"Make more",onclick:()=>bump(1)},"+");
  function bump(d){
    let v=mult;
    v=d>0?(v<1?Math.min(1,v*2):Math.min(20,Math.floor(v+1e-9)+1)):(v<=1?Math.max(.25,v/2):Math.ceil(v-1e-9)-1);
    mult=Math.round(v*100)/100;paint();
  }
  const mcb=el("input",{type:"checkbox"});mcb.checked=metric;
  mcb.onchange=()=>{metric=mcb.checked;store.set("metric",metric?"1":"0");paint()};

  function paint(){
    out.textContent=fmt(mult)+"×";down.disabled=mult<=.25;up.disabled=mult>=20;
    serv.replaceChildren(...(r.servings?["Serves ",...scaleText(r.servings,mult)]
      :r.servings_num?["Serves "+fmt(r.servings_num*mult)+(r.servings_inferred&&mult===1?" (estimated)":"")]:[]));
    ingList.replaceChildren(...(r.ingredients||[]).map(x=>{
      if(isHd(x))return el("li",{class:"hd"},hdText(x));
      const sp=ingSplit(x,mult,metric);
      const li=el("li",{class:have.has(x)?"have":"",role:"checkbox","aria-checked":String(have.has(x)),tabindex:"0"},
        el("span",{class:"am"},...sp[0]),el("span",{},...sp[1]));
      const flip=()=>{have.has(x)?have.delete(x):have.add(x);li.classList.toggle("have",have.has(x));li.setAttribute("aria-checked",String(have.has(x)))};
      li.onclick=flip;li.onkeydown=e=>{if(e.key===" "||e.key==="Enter"){e.preventDefault();flip()}};
      return li}));
    stepList.replaceChildren(...(r.steps||[]).map(s=>isHd(s)?el("li",{class:"hd"},hdText(s)):el("li",{},tempText(s,metric))));
  }
  paint();

  const nut=r.nutrition&&Object.keys(r.nutrition).length?el("details",{class:"nut"},el("summary",{},"Nutrition facts"),
    el("table",{},el("tbody",{},Object.entries(r.nutrition).map(([k,v])=>el("tr",{},el("td",{},k),el("td",{},v)))))):null;
  const hasIng=(r.ingredients||[]).length,hasSteps=(r.steps||[]).some(s=>!isHd(s));

  root.className="";root.removeAttribute("aria-busy");
  root.replaceChildren(hero||"",el("div",{class:"body"},
    el("h1",{},r.title||"Untitled recipe"),meta,timesLine(r)||"",
    hasIng?el("div",{class:"tools"},"Scale",el("span",{class:"stepper"},down,out,up),el("label",{class:"chk"},mcb,"Metric")):"",
    hasIng?el("h2",{},"Ingredients"):"",hasIng?ingList:"",
    hasSteps?el("h2",{},"Directions"):"",hasSteps?stepList:"",
    nut||"",
    el("p",{class:"foot"},"Shared from Foodbox · view only")));
  // the class names timesLine() gives in the app
  const t=root.querySelector(".times");if(t)t.classList.remove("cards");
  if(hasSteps)document.body.append(el("div",{class:"dock"},el("button",{class:"btn pri",onclick:()=>cook(r,mult,metric)},"Start cooking")));
  else document.body.style.paddingBottom="0";
})();
