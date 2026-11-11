// Runs in <head> so the page never flashes the wrong theme. Light by default; the switch saves "theme" (same key as the app).
try{if(localStorage.getItem("theme")==="dark")document.documentElement.dataset.theme="dark"}catch(e){}
