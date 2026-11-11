// Public, read-only recipe page at /s/<token>. Nothing here writes to the server: the only requests are GETs.
// Scale, Metric, theme and ticked-off ingredients are view settings kept in this browser only.
(async()=>{
  const token=location.pathname.split("/")[2]||"",root=document.getElementById("app"),H=document.documentElement;
  const mq=matchMedia("(min-width:860px)"),setDT=()=>H.classList.toggle("dt",mq.matches);
  setDT();mq.addEventListener("change",setDT);

  // light by default; the switch remembers the choice (same "theme" key as the app)
  const SUN=["M12 8a4 4 0 100 8 4 4 0 000-8z","M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"],MOON="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z";
  const svg=d=>{const n="http://www.w3.org/2000/svg",v=document.createElementNS(n,"svg");v.setAttribute("viewBox","0 0 24 24");v.setAttribute("width","20");v.setAttribute("height","20");
    v.setAttribute("fill","none");v.setAttribute("stroke","currentColor");v.setAttribute("stroke-width","2");v.setAttribute("stroke-linecap","round");v.setAttribute("stroke-linejoin","round");v.setAttribute("aria-hidden","true");
    for(const x of [].concat(d)){const p=document.createElementNS(n,"path");p.setAttribute("d",x);v.append(p)}return v};
  const themeBtn=el("button",{type:"button",class:"theme"});
  const paintTheme=()=>{const dark=H.dataset.theme==="dark",l=dark?"Switch to light mode":"Switch to dark mode";
    themeBtn.replaceChildren(svg(dark?SUN:MOON));themeBtn.setAttribute("aria-label",l);themeBtn.title=l;
    document.querySelector('meta[name="theme-color"]').setAttribute("content",dark?"#0f0f11":"#f2f2f7")};
  themeBtn.onclick=()=>{const dark=H.dataset.theme!=="dark";if(dark)H.dataset.theme="dark";else delete H.dataset.theme;store.set("theme",dark?"dark":"light");paintTheme()};
  paintTheme();document.body.append(themeBtn);

  const fail=(t,s)=>{root.className="";root.removeAttribute("aria-busy");document.body.classList.add("nosteps");root.replaceChildren(el("div",{class:"msg"},el("b",{},t),s))};
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

  const intro=el("header",{class:"intro"});
  const hero=r.cover?el("img",{class:"hero",src:r.cover,alt:"",referrerpolicy:"no-referrer",onerror:e=>{e.target.remove();intro.classList.add("nopic")}}):null;
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
  const times=timesLine(r);if(times)times.classList.remove("cards");
  // phones: a bar pinned to the bottom; desktop: a normal button under the title (same element, CSS decides)
  const dock=hasSteps?el("div",{class:"dock"},el("button",{class:"btn pri",onclick:()=>cook(r,mult,metric)},"Start cooking")):null;
  if(!hasSteps)document.body.classList.add("nosteps");

  if(hero)intro.append(hero);else intro.classList.add("nopic");
  intro.append(el("div",{class:"txt"},el("h1",{},r.title||"Untitled recipe"),meta,times||"",dock||""));
  const colA=hasIng?el("section",{class:"colA","aria-label":"Ingredients"},
    el("h2",{},"Ingredients"),
    el("div",{class:"tools"},"Scale",el("span",{class:"stepper"},down,out,up),el("label",{class:"chk"},mcb,"Metric")),ingList):null;
  const colB=hasSteps||nut?el("section",{class:"colB","aria-label":"Directions"},hasSteps?el("h2",{},"Directions"):"",hasSteps?stepList:"",nut||""):null;
  const cols=el("div",{class:"cols"+(colA&&colB?"":" one")},colA||"",colB||"",el("p",{class:"foot"},"Shared from Foodbox · view only"));

  root.className="";root.removeAttribute("aria-busy");
  root.replaceChildren(intro,cols);
})();
