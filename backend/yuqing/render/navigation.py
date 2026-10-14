"""Self-contained report navigation, derived from rendered structural headings."""

NAVIGATION_CSS = """
:root{--toc-width:268px;--tools-height:58px;--report-blue:#315bd7;--chapter-frame:#adc4f2}
.report-tools{padding:10px 20px;box-shadow:0 2px 10px #172b4d0c}
.report-document{min-width:0;margin-left:var(--toc-width);padding:0 clamp(20px,3vw,44px)}
.report-document>header,.report-quality{width:100%;max-width:var(--report-width);margin-left:auto;margin-right:auto}
.report-document>header{background:#fff;color:#172b4d;border-bottom:1px solid #dce4ef;padding:32px 34px 28px}
.report-shell{display:block;width:100%;max-width:var(--report-width);margin:0 auto 80px;padding:0}
main{width:100%;margin:0;border-top-color:#dce4ef;box-shadow:0 8px 28px #172b4d0a}
.report-section{scroll-margin-top:calc(var(--tools-height) + 24px)}
.report-section.is-current{outline:3px solid var(--chapter-frame);outline-offset:3px}
.report-subheading{scroll-margin-top:calc(var(--tools-height) + 24px)}
.report-toc{position:fixed;inset:var(--tools-height) auto 0 0;width:var(--toc-width);height:calc(100dvh - var(--tools-height));margin:0;align-self:auto;justify-self:auto;z-index:25;display:flex;flex-direction:column;background:#f1f4fa;color:#172b4d;border-right:1px solid #dce4ef;border-radius:0;box-shadow:none;overflow:hidden}
.toc-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:22px 18px 16px;border-bottom:1px solid #dce4ef}
.toc-head h2{margin:0;font:700 16px/1.5 'Microsoft YaHei UI','PingFang SC',sans-serif;color:#172b4d}
.toc-head button{width:30px;height:30px;border:0;border-radius:5px;background:transparent;color:#56677d;cursor:pointer;font-size:22px;line-height:1}
.toc-head button:hover{background:#e1e8f5;color:var(--report-blue)}
.toc-scroll{min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;padding:12px 10px 24px}
.report-toc ol{display:block;list-style:none;margin:0;padding:0}
.toc-chapter{margin:3px 0}
.toc-row{display:flex;align-items:center;gap:2px}
.report-toc a{display:flex;align-items:start;gap:8px;min-width:0;padding:9px 10px;color:#445570;border:0;border-radius:5px;text-decoration:none;font-size:13px;line-height:1.55;overflow-wrap:anywhere}
.report-toc .toc-row>a{flex:1}
.report-toc a span{flex:0 0 23px;font:600 11px/1.8 Consolas,monospace;color:#75839b}
.report-toc a:hover{background:#e7edf8;color:#2448ae}
.report-toc a[aria-current="location"]{background:#e0e9fc;color:#2448ae;font-weight:600;border:0}
.report-toc a[aria-current="location"] span{color:#315bd7}
.toc-expand{flex:0 0 28px;height:30px;padding:0;border:0;border-radius:4px;background:transparent;color:#657792;cursor:pointer}
.toc-expand::before{content:'›';display:block;font-size:20px;line-height:1;transform:rotate(0deg)}
.toc-expand[aria-expanded="true"]::before{transform:rotate(90deg)}
.toc-expand:hover{background:#e1e8f5;color:var(--report-blue)}
.report-toc .toc-children{margin:2px 8px 9px 21px;padding-left:8px;border-left:1px solid #d4deef}
.report-toc .toc-children a{padding:7px 9px;font-size:12px;font-weight:400}
.report-toc .toc-children a[aria-current="location"]{font-weight:600}
.report-toc [hidden]{display:none!important}
.report-toc a:focus-visible,.report-toc button:focus-visible{outline:2px solid var(--report-blue);outline-offset:2px}
.toc-backdrop{display:none}
.toc-drawer-open .back-to-top{visibility:hidden}
.toc-collapsed .report-toc{visibility:hidden;transform:translateX(-100%)}
.toc-collapsed .report-document{margin-left:0}
@media(max-width:1000px){
  .report-document,.toc-collapsed .report-document{margin-left:0;padding:0 18px}
  .report-document>header{padding:26px 22px}
  .report-toc{width:min(300px,calc(100vw - 48px));background:#f7f9fd;box-shadow:12px 0 30px #172b4d1a}
  .toc-drawer-open{overflow:hidden}
  .toc-drawer-open .toc-backdrop{display:block;position:fixed;inset:var(--tools-height) 0 0;z-index:24;background:#172b4d40;border:0;padding:0;cursor:pointer}
}
@media(max-width:600px){
  .report-tools{padding:8px;gap:6px}
  .report-document,.toc-collapsed .report-document{padding:0 14px}
  .report-document>header{padding:24px 18px}
  .report-quality{width:100%}
}
@media print{
  :root,body{background:#fff}
  body{overflow:visible!important}
  .report-toc,.toc-backdrop,.toc-button{display:none!important}
  .report-document,.toc-collapsed .report-document{margin:0;padding:0}
  .report-document>header,.report-quality,.report-shell,main{width:100%;max-width:none;margin:0}
  .report-document>header{padding:12px 0 20px}
  main{box-shadow:none;border:0}
  .report-section.is-current{outline:none}
}
"""

NAVIGATION_SCRIPT = r"""
const reportSections=[...document.querySelectorAll('.report-section')];
const chapterLinks=[...document.querySelectorAll('.report-toc a[data-section-target]')];
const toc=document.querySelector('.report-toc');
const tocButton=document.querySelector('.toc-button');
const tocBackdrop=document.querySelector('.toc-backdrop');
const tools=document.querySelector('.report-tools');
const documentPane=document.querySelector('.report-document');
const narrowScreen=window.matchMedia('(max-width:1000px)');
let currentChapter='';
let navigationFrame=0;

reportSections.forEach(section=>{
  const list=document.getElementById(`${section.id}-toc-children`);
  const headings=[...section.querySelectorAll('h2:not(.sr-only),h3')].filter(heading=>
    !heading.closest('article,details,aside') &&
    (heading.tagName==='H2'||heading.parentElement.tagName==='SECTION') && heading.textContent.trim());
  headings.forEach((heading,index)=>{
    if(!heading.id)heading.id=`${section.id}-heading-${index+1}`;
    heading.classList.add('report-subheading');
    const item=document.createElement('li');
    const link=document.createElement('a');
    link.href=`#${heading.id}`;
    link.dataset.headingTarget=heading.id;
    link.dataset.chapterTarget=section.id;
    link.textContent=heading.textContent.trim();
    item.append(link);list.append(item);
  });
});

const setChapterExpanded=(id,expanded)=>{
  const button=document.querySelector(`[data-nav-toggle="${id}"]`);
  const list=document.getElementById(`${id}-toc-children`);
  list.hidden=!expanded;button.setAttribute('aria-expanded',String(expanded));
  button.setAttribute('aria-label',`${expanded?'收起':'展开'}${button.dataset.chapterLabel}的小节`);
};
const setTocOpen=(open,restoreFocus=false)=>{
  document.body.classList.toggle('toc-collapsed',!open);
  document.body.classList.toggle('toc-drawer-open',open&&narrowScreen.matches);
  toc.inert=!open;toc.setAttribute('aria-hidden',String(!open));
  documentPane.inert=open&&narrowScreen.matches;
  tools.inert=open&&narrowScreen.matches;
  tocButton.setAttribute('aria-expanded',String(open));
  tocButton.textContent=open&&!narrowScreen.matches?'收起目录':'目录';
  if(open&&narrowScreen.matches){
    toc.setAttribute('role','dialog');toc.setAttribute('aria-modal','true');
    toc.querySelector('.toc-close').focus();
  }else{
    toc.removeAttribute('role');toc.removeAttribute('aria-modal');
    if(restoreFocus)tocButton.focus();
  }
};
tocButton.addEventListener('click',()=>setTocOpen(document.body.classList.contains('toc-collapsed')));
toc.querySelector('.toc-close').addEventListener('click',()=>setTocOpen(false,true));
tocBackdrop.addEventListener('click',()=>setTocOpen(false,true));
narrowScreen.addEventListener('change',()=>{setTocOpen(!narrowScreen.matches,true);scheduleNavigation();});
document.querySelectorAll('[data-nav-toggle]').forEach(button=>button.addEventListener('click',()=>
  setChapterExpanded(button.dataset.navToggle,button.getAttribute('aria-expanded')!=='true')));
document.addEventListener('keydown',event=>{
  if(!document.body.classList.contains('toc-drawer-open'))return;
  if(event.key==='Escape'){event.preventDefault();setTocOpen(false,true);}
  if(event.key==='Tab'){
    const focusable=[...toc.querySelectorAll('button,a')].filter(item=>item.getClientRects().length&&!item.hidden);
    const first=focusable[0],last=focusable[focusable.length-1];
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
});

const activate=(id,headingId='')=>{
  const changed=id!==currentChapter;currentChapter=id;
  chapterLinks.forEach(link=>{
    if(link.dataset.sectionTarget===id)link.setAttribute('aria-current','location');
    else link.removeAttribute('aria-current');
  });
  reportSections.forEach(section=>{
    section.classList.toggle('is-current',section.id===id&&!section.classList.contains('is-hidden'));
    if(changed)setChapterExpanded(section.id,section.id===id);
  });
  toc.querySelectorAll('[data-heading-target]').forEach(link=>{
    if(link.dataset.headingTarget===headingId)link.setAttribute('aria-current','location');
    else link.removeAttribute('aria-current');
  });
  if(changed){
    const link=chapterLinks.find(item=>item.dataset.sectionTarget===id);
    const scroller=toc.querySelector('.toc-scroll');
    if(link&&scroller){
      const bounds=link.getBoundingClientRect(),pane=scroller.getBoundingClientRect();
      if(bounds.top<pane.top||bounds.bottom>pane.bottom)scroller.scrollTop+=bounds.top-pane.top-12;
    }
  }
};
const syncReportNavigation=()=>{
  navigationFrame=0;
  const visible=reportSections.filter(section=>!section.classList.contains('is-hidden'));
  const readingLine=tools.getBoundingClientRect().height+40;
  const current=visible.filter(section=>section.getBoundingClientRect().top<=readingLine).at(-1)||visible[0];
  if(!current)return;
  const heading=[...current.querySelectorAll('.report-subheading')].filter(item=>
    item.getClientRects().length&&item.getBoundingClientRect().top<=readingLine).at(-1);
  activate(current.id,heading?.id||'');
};
const scheduleNavigation=()=>{
  if(!navigationFrame)navigationFrame=requestAnimationFrame(syncReportNavigation);
};
updateNavigation=()=>{
  reportSections.forEach(section=>{
    const list=document.getElementById(`${section.id}-toc-children`);
    list.querySelectorAll('a').forEach(link=>{
      const heading=document.getElementById(link.dataset.headingTarget);
      link.closest('li').classList.toggle('is-hidden',!heading.getClientRects().length);
    });
    const expandable=[...list.children].some(item=>!item.classList.contains('is-hidden'));
    document.querySelector(`[data-nav-toggle="${section.id}"]`).hidden=!expandable;
  });
  scheduleNavigation();
};
toc.querySelectorAll('a').forEach(link=>link.addEventListener('click',event=>{
  event.preventDefault();
  const id=link.dataset.headingTarget||link.dataset.sectionTarget;
  const target=document.getElementById(id);
  if(!target)return;
  history.pushState(null,'',link.getAttribute('href'));
  activate(link.dataset.chapterTarget||id,link.dataset.headingTarget||'');
  if(narrowScreen.matches)setTocOpen(false,true);
  target.scrollIntoView({block:'start'});
  scheduleNavigation();
}));
window.addEventListener('scroll',scheduleNavigation,{passive:true});
window.addEventListener('resize',scheduleNavigation);
const updateToolsHeight=()=>{
  document.documentElement.style.setProperty('--tools-height',`${tools.getBoundingClientRect().height}px`);
  scheduleNavigation();
};
new ResizeObserver(updateToolsHeight).observe(tools);
setTocOpen(!narrowScreen.matches);
updateToolsHeight();
"""
