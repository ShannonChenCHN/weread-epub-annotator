#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inject_annotate.py —— 给 weread-epub-preview 生成的 index.html 注入「选中即批注」层。

用法:
    python3 inject_annotate.py <预览目录/index.html> [--undo]

特性:
  * 不修改 EPUB 源，只改预览页 HTML（--undo 可完整移除，可逆）。
  * 注入后：顶栏出现「✏️ 标注」「📋 批注(n)」两个按钮。
      - 打开「✏️ 标注」模式 → 在正文中用鼠标选中文字/图片 → 弹出批注框
        （严重级 P1/P2/P3 + 类型 文字/图片/版式 + 备注）→ 保存。
      - 批注按「源 xhtml 文件路径 + 文字片段 + 最近标题」定位，存入 localStorage，
        换章自动重绘常驻标记（MutationObserver）。
      - 「📋 批注」面板可：点条目跳转定位并闪烁高亮、导出 JSON、导出「给 agent 的 MD 任务清单」、清空。
  * 锚点不依赖源码 id（源码正文元素本就无 id）；靠文字片段 + 最近标题即可反查源 xhtml。

设计要点:
  * 注入三段（CSS / HTML / JS），每段用注释标记包裹，--undo 按标记剥离，故可重复运行。
  * 标注逻辑只读预览页里既有的 #wr-data（含 chapters[i].file 源路径）与 open Shadow DOM，
    不触碰 weread_preview.py 的生成器，也不碰 EPUB。
  * **高亮样式注入 Shadow 内**：light-DOM 的 CSS 穿透不了 Shadow DOM 边界，故高亮规则
    必须在 shadow 里（#wr-anno-shadow）。引擎每次 build() 会清空 shadow，用 MutationObserver 重建后重注。
  * **切章不依赖引擎内部函数**：目录 #toc 在 light DOM，<a data-i> 点击即触发引擎 mount(idx)
    （同步重建 shadow），故跨章定位只需模拟点击对应 TOC 条目。
"""

import argparse
import re
import sys
from pathlib import Path

MARK = "wr-anno"
CSS_BEGIN, CSS_END = f"<!-- {MARK}:css -->", f"<!-- /{MARK}:css -->"
HTML_BEGIN, HTML_END = f"<!-- {MARK}:html -->", f"<!-- /{MARK}:html -->"
JS_BEGIN, JS_END = f"<!-- {MARK}:js -->", f"<!-- /{MARK}:js -->"

CSS = r"""
<!-- wr-anno:css -->
<style id="wr-anno-style">
.anno-pop{position:fixed;z-index:9999;background:#fff;color:#1c1f24;border:1px solid #e3e5e9;
  border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.22);padding:10px;width:288px;
  font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.anno-pop textarea{width:100%;height:64px;resize:vertical;border:1px solid #e3e5e9;border-radius:6px;
  padding:6px;font:inherit;box-sizing:border-box}
.anno-pop .row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;align-items:center}
.anno-pop .seg{display:flex;gap:4px}
.anno-pop .seg button{border:1px solid #e3e5e9;background:#fff;border-radius:6px;padding:3px 8px;cursor:pointer;font:inherit}
.anno-pop .seg button.on{background:#3b6df5;color:#fff;border-color:#3b6df5}
.anno-pop .act{margin-left:auto;display:flex;gap:6px}
.anno-pop .save{background:#3b6df5;color:#fff;border:1px solid #3b6df5;border-radius:6px;padding:4px 12px;cursor:pointer;font:inherit}
.anno-pop .cancel{background:#fff;border:1px solid #e3e5e9;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
.anno-pop .meta{color:#8b9098;font-size:11px}
/* 顶栏「标注」按钮：选中态与「说明」按钮 .mtoggle[aria-pressed=true] 完全一致 */
.btn-anno-mode[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);background:rgba(59,109,245,.08)}
/* 顶栏按钮内的 SVG 图标：与文字基线对齐、随按钮文字色（含选中态）变化 */
.topbar .btn svg{width:14px;height:14px;vertical-align:-2px;margin-right:4px}
.anno-panel{position:fixed;top:0;right:0;height:100%;width:330px;background:#fff;color:#1c1f24;
  border-left:1px solid #e3e5e9;box-shadow:-8px 0 24px rgba(0,0,0,.12);z-index:9998;display:none;
  flex-direction:column;font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.anno-panel.open{display:flex}
.anno-panel header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #e3e5e9}
.anno-panel header b{font-size:14px}
.anno-panel .tools{margin-left:auto;display:flex;gap:6px}
.anno-panel .tools button{border:1px solid #e3e5e9;background:#fff;border-radius:6px;padding:3px 8px;cursor:pointer;font:inherit}
.anno-panel .tools button:hover{border-color:#3b6df5;color:#3b6df5}
.anno-panel .list{overflow:auto;padding:8px 10px 40px;flex:1}
.anno-grp{margin:0 0 10px}
.anno-grp h4{margin:6px 2px;font-size:12px;color:#61666e;word-break:break-all}
.anno-item{position:relative;border:1px solid #e3e5e9;border-radius:8px;padding:7px 54px 7px 9px;margin:0 0 7px;cursor:pointer}
.anno-item:hover{background:#f6f8ff}
.anno-item .badge{display:inline-block;font-size:11px;padding:0 7px;border-radius:99px;color:#61666e;background:#eef1f6;margin-right:6px}
.anno-item .snip{color:#1c1f24;font-weight:600}
.anno-item .cm{color:#61666e;margin-top:3px;white-space:pre-wrap;word-break:break-word}
.anno-item .metaline{color:#8b9098;font-size:11px;margin-top:3px}
/* 每条右侧编辑/删除按钮：悬浮显现，点按钮不触发整条跳转 */
.anno-item .acts{position:absolute;top:6px;right:6px;display:flex;gap:4px;opacity:0;transition:opacity .15s}
.anno-item:hover .acts{opacity:1}
.anno-item .acts button{display:inline-flex;align-items:center;justify-content:center;border:1px solid #e3e5e9;background:#fff;border-radius:6px;width:24px;height:24px;padding:0;cursor:pointer;color:#61666e}
.anno-item .acts button:hover{border-color:#3b6df5;color:#3b6df5}
.anno-item .acts button.act-del:hover{border-color:#e5484d;color:#e5484d}
.anno-item .acts svg{display:block;width:14px;height:14px}
.anno-empty{color:#8b9098;text-align:center;margin-top:30px;line-height:1.8}
@media (max-width:900px){
  /* 窄屏抽屉化：从右侧滑入 + 半透明蒙层（与目录抽屉交互一致） */
  .anno-panel{width:min(82vw,300px);display:flex;transform:translateX(100%);transition:transform .22s ease}
  .anno-panel.open{transform:none}
  .anno-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.38);z-index:35}
}
</style>
<!-- /wr-anno:css -->
"""

HTML = r"""
<!-- wr-anno:html -->
<div class="anno-backdrop" id="annoBackdrop"></div>
<aside class="anno-panel" id="annoPanel">
  <header>
    <b>批注</b>
    <span class="tools">
      <button id="annoExportJson" title="下载 annotations.json">JSON</button>
      <button id="annoExportMd" title="下载给 agent 的任务清单">导出MD</button>
      <button id="annoClear" title="清空本书全部批注">清空</button>
      <button class="panel-close" id="annoClose" title="关闭">✕</button>
    </span>
  </header>
  <div class="list" id="annoList"></div>
</aside>
<div class="anno-pop" id="annoPop" style="display:none">
  <textarea id="annoText" placeholder="这里写要改什么…（可留空，仅标记位置）"></textarea>
  <div class="row">
    <div class="seg" id="annoType">
      <button data-v="文字" class="on">文字</button><button data-v="图片">图片</button><button data-v="版式">版式</button>
    </div>
  </div>
  <div class="row">
    <span class="meta" id="annoLoc"></span>
    <span class="act">
      <button class="cancel" id="annoCancel">取消</button>
      <button class="save" id="annoSave">保存</button>
    </span>
  </div>
</div>
<!-- /wr-anno:html -->
"""

JS = r"""
<!-- wr-anno:js -->
<script>
(function(){
  'use strict';
  var LS_KEY='wr-anno-v1';
  var STORE=(function(){try{var s=window.localStorage;return{get:function(k){try{return s.getItem(k);}catch(e){return null;}},set:function(k,v){try{s.setItem(k,v);}catch(e){}}};}catch(e){return{get:function(){return null;},set:function(){}};}})();
  function genId(){return 'a'+Date.now().toString(36)+Math.random().toString(36).slice(2,7);}
  function load(){
    var a; try{a=JSON.parse(STORE.get(LS_KEY)||'[]')||[];}catch(e){a=[];}
    /* 迁移：给旧批注（无 id）补 id，便于编辑/删除按 id 精确命中 */
    var changed=false;
    for(var i=0;i<a.length;i++){ if(!a[i].id){ a[i].id=genId(); changed=true; } }
    if(changed) save(a);
    return a;
  }
  function save(a){STORE.set(LS_KEY,JSON.stringify(a));}

  var HOST=document.getElementById('wr-host');
  function getSR(){ return HOST && HOST.shadowRoot; }
  var SHADOW=getSR();
  var DATA=JSON.parse(document.getElementById('wr-data').textContent);

  /* 高亮样式必须注入 Shadow 内：light-DOM 的 CSS 穿透不了 Shadow DOM 边界，
     这正是之前"整块高亮看不到"的根因。引擎每次 build() 会清空 shadow 子节点，
     故用 MutationObserver 在重建后再注入。 */
  var ANNO_SHADOW_CSS =
    '.wr-anno-mark{box-shadow:inset 3px 0 0 #3b6df5}' +
    '.wr-anno-flash{background:#ffd54a;border-radius:2px;animation:wrAnnoFlash 2.6s ease-out forwards}' +
    '.wr-anno-flash-block{animation:wrAnnoFlash 2.6s ease-out forwards}' +
    '@keyframes wrAnnoFlash{0%{background:#ffd54a}55%{background:#ffe9a8}100%{background:rgba(255,213,74,0)}}';
  function ensureShadowCss(){
    var s=getSR(); if(!s) return;
    var st=s.querySelector('#wr-anno-shadow');
    if(!st){ st=document.createElement('style'); st.id='wr-anno-shadow'; s.appendChild(st); }
    if(st.textContent!==ANNO_SHADOW_CSS) st.textContent=ANNO_SHADOW_CSS;
  }

  /* Shadow DOM 选区：优先 shadowRoot.getSelection()，拿不到再退回 window.getSelection() */
  function getSel(){
    var s=getSR();
    if(s && typeof s.getSelection==='function'){ try{ var x=s.getSelection(); if(x && x.rangeCount) return x; }catch(e){} }
    return window.getSelection();
  }
  /* 当前章索引：引擎在 section 上写 data-idx；缺 data-idx 的旧预览用标题匹配兜底 */
  function chapterIdxOf(sec){
    if(!sec) return -1;
    if(sec.dataset && sec.dataset.idx!=null && sec.dataset.idx!=='') return +sec.dataset.idx;
    var h=sec.querySelector('h1,h2,h3,h4'); var t=h?h.textContent.trim():'';
    if(t){ for(var i=0;i<DATA.chapters.length;i++){ var ct=(DATA.chapters[i].title||'').trim(); if(ct && (ct===t||t.indexOf(ct)>=0||ct.indexOf(t)>=0)) return i; } }
    return -1;
  }

  var annoMode=true, curType='文字', pendingCtx=null, editingId=null;
  var bList=null;
  /* 内联 SVG 图标（Lucide/Feather 风，stroke=currentColor，随 hover 变色，无需外部资源） */
  var SVG_EDIT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
  var SVG_DEL='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';
  var SVG_LIST='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>';

  /* ---- 顶栏按钮（兼容新旧两版顶栏，直接挂到 .topbar，窄屏也可见） ---- */
  var topbar=document.querySelector('.topbar');
  if(topbar){
    var bMode=document.createElement('button'); bMode.className='btn btn-anno-mode'; bMode.innerHTML=SVG_EDIT+' 标注';
    bMode.style.marginLeft='auto';
    bMode.setAttribute('aria-pressed', annoMode ? 'true' : 'false');
    bList=document.createElement('button'); bList.className='btn'; bList.innerHTML=SVG_LIST+' 批注(0)';
    bMode.onclick=function(){annoMode=!annoMode; bMode.setAttribute('aria-pressed', annoMode ? 'true' : 'false'); if(!annoMode) hidePop();};
    bList.onclick=function(){document.body.classList.remove('navopen','infoopen'); var p=document.getElementById('annoPanel'); p.classList.toggle('open'); renderList(); syncBackdrop();};
    topbar.appendChild(bMode); topbar.appendChild(bList);
  }

  /* ---- 工具 ---- */
  function closest(el,sel){while(el&&el!==SHADOW){if(el.matches&&el.matches(sel))return el; el=el.parentElement;} return null;}
  function blockOf(node){return closest(node,'p,div,h1,h2,h3,h4,h5,h6,figure,li,blockquote,table,.fig-c');}
  function secOf(node){return closest(node,'.wr-sec');}
  function nearestHeading(block,sec){
    if(!block||!sec)return null;
    var hs=sec.querySelectorAll('h1,h2,h3,h4'); var best=null;
    for(var i=0;i<hs.length;i++){ if(hs[i].compareDocumentPosition(block)&Node.DOCUMENT_POSITION_FOLLOWING) best=hs[i]; }
    return best?best.textContent.trim():null;
  }
  function figIndexOf(block,sec){
    if(!block||!sec)return null;
    var figs=sec.querySelectorAll('figure,.fig-c'); var n=0;
    for(var i=0;i<figs.length;i++){ if(figs[i].compareDocumentPosition(block)&Node.DOCUMENT_POSITION_FOLLOWING) break; n++; }
    return n;
  }
  function snippetOf(t){t=(t||'').replace(/\s+/g,' ').trim(); return t.length>60?t.slice(0,60):t;}

  /* ---- 选区 -> 批注上下文 ---- */
  function onSelect(){
    editingId=null;   /* 进入新建态，避免复用上一次的编辑态 */
    if(!annoMode)return;
    var sel=getSel();
    if(!sel||sel.isCollapsed||!sel.rangeCount){hidePop();return;}
    var range=sel.getRangeAt(0);
    var node=range.commonAncestorContainer;
    if(node.nodeType===3)node=node.parentElement;
    var S=getSR(); if(!S||!S.contains(node)){hidePop();return;}
    var sec=secOf(node); if(!sec){hidePop();return;}
    var idx=chapterIdxOf(sec); var ch=DATA.chapters[idx]; if(!ch){hidePop();return;}
    var block=blockOf(node);
    var isImg=!!closest(node,'figure,img,.fig-c');
    if(isImg&&curType==='文字')setType('图片');
    pendingCtx={file:ch.file, chapterTitle:ch.title, chapterIdx:idx,
      snippet:snippetOf(sel.toString()),
      selectedText:(sel.toString()||'').slice(0,200),
      context:{nearestHeading:nearestHeading(block,sec), elemTag:block?block.tagName.toLowerCase():'',
        elemClass:block&&block.className?(''+block.className):'', figIndex:figIndexOf(block,sec)}};
    var pop=document.getElementById('annoPop'); pop.style.display='block';
    document.getElementById('annoSave').textContent='保存';
    var r=range.getBoundingClientRect();
    var x=Math.min(r.left, window.innerWidth-300), y=r.bottom+8;
    if(y>window.innerHeight-220)y=r.top-226;
    pop.style.left=Math.max(8,x)+'px'; pop.style.top=Math.max(8,y)+'px';
    document.getElementById('annoLoc').textContent=(ch.title||'')+' · '+(pendingCtx.context.nearestHeading||'(章首)');
    document.getElementById('annoText').focus();
  }
  function hidePop(){var p=document.getElementById('annoPop'); if(p)p.style.display='none'; pendingCtx=null; editingId=null;}

  function setType(v){curType=v; markSeg('annoType',v);}
  function markSeg(id,v){var box=document.getElementById(id); if(!box)return; var bs=box.querySelectorAll('button'); for(var i=0;i<bs.length;i++){bs[i].classList.toggle('on',bs[i].dataset.v===v);}}

  document.getElementById('annoType').addEventListener('click',function(e){if(e.target.dataset&&e.target.dataset.v)setType(e.target.dataset.v);});
  document.getElementById('annoCancel').onclick=hidePop;
  document.getElementById('annoSave').onclick=function(){
    var text=document.getElementById('annoText').value.trim();
    var annos=load();
    if(editingId){
      /* 编辑态：按 id 原地更新备注 + 类型（不改锚点/位置） */
      for(var k=0;k<annos.length;k++){ if(annos[k].id===editingId){ annos[k].comment=text; annos[k].type=curType; break; } }
      save(annos); editingId=null; hidePop();
    } else {
      /* 新建态 */
      if(!pendingCtx)return;
      annos.push({id:genId(), file:pendingCtx.file, chapterTitle:pendingCtx.chapterTitle, chapterIdx:pendingCtx.chapterIdx,
        snippet:pendingCtx.snippet, selectedText:pendingCtx.selectedText, context:pendingCtx.context,
        comment:text, type:curType, createdAt:new Date().toISOString()});
      save(annos); hidePop();
      if(window.getSelection)window.getSelection().removeAllRanges();
    }
    ensureShadowCss(); applyHighlights(); renderList(); updateCount();
  };

  /* 选区监听：等 shadow 建立后再挂（capture 保证拿到选区）；引擎很少重建 shadowRoot，故监听长期有效 */
  (function bindSel(){
    var s=getSR();
    if(!s){ setTimeout(bindSel,80); return; }
    if(s.__annoBound) return; s.__annoBound=true;
    s.addEventListener('mouseup', onSelect, true);
  })();

  /* ---- 定位 / 常驻标记（左侧按严重级上色的细条，做总览） ---- */
  function locateEl(anno){
    var S=getSR(); var sec=S&&S.querySelector('.wr-secs .wr-sec');
    if(!sec)return null;
    if(anno.chapterIdx!=null&&chapterIdxOf(sec)!==anno.chapterIdx)return null;
    /* 定位串优先级：60 字 snippet → 完整 selectedText（更长的原文更易命中）；
       任一枚活即返回，避免「匹配不到整条丢失」。 */
    var needles=[];
    if(anno.snippet) needles.push(anno.snippet);
    if(anno.selectedText && anno.selectedText!==anno.snippet) needles.push(anno.selectedText);
    if(needles.length){
      var blocks=sec.querySelectorAll('p,div,h1,h2,h3,h4,h5,h6,figure,li,blockquote,table,.fig-c');
      for(var n=0;n<needles.length;n++){
        var cand=[];
        for(var i=0;i<blocks.length;i++){var t=blocks[i].textContent||''; if(t.indexOf(needles[n])>=0)cand.push(blocks[i]);}
        if(anno.context&&anno.context.nearestHeading){
          for(var j=0;j<cand.length;j++){ if(nearestHeading(cand[j],sec)===anno.context.nearestHeading) return cand[j]; }
        }
        if(cand.length)return cand[0];
      }
    }
    if(anno.type==='图片'&&anno.context&&anno.context.figIndex!=null){
      var figs=sec.querySelectorAll('figure,.fig-c'); if(figs[anno.context.figIndex])return figs[anno.context.figIndex];
    }
    return null;
  }
  function applyHighlights(){
    var S=getSR(); if(!S)return;
    ensureShadowCss();   /* 切章 build() 会清空 Shadow 内的高亮样式，常驻标记前先补回 */
    var marks=S.querySelectorAll('.wr-anno-mark');
    for(var i=0;i<marks.length;i++){marks[i].classList.remove('wr-anno-mark');}
    load().forEach(function(a){var el=locateEl(a); if(el)el.classList.add('wr-anno-mark');});
  }
  /* 换章/重建后：先重注 shadow CSS（build 会清空），再重绘常驻标记 */
  (function watchSR(){
    var s=getSR();
    if(!s){ setTimeout(watchSR,80); return; }
    if(s.__annoWatch) return; s.__annoWatch=true;
    new MutationObserver(function(){ensureShadowCss();applyHighlights();}).observe(s,{childList:true,subtree:true});
  })();

  /* ---- 点击条目：跨章定位 + 滚动 + 精确子串闪烁 ---- */
  function goChapter(idx){
    var toc=document.getElementById('toc'); if(!toc) return;
    var as=toc.querySelectorAll('a[data-i]');
    for(var i=0;i<as.length;i++){
      var lo=parseInt(as[i].getAttribute('data-i'),10);
      var hi=as[i].getAttribute('data-last'); hi=(hi==null)?lo:parseInt(hi,10);
      if(idx>=lo && idx<=hi){ try{as[i].click();}catch(e){} return; }
    }
  }
  function unwrapFlash(){
    var s=getSR(); if(!s) return;
    var marks=s.querySelectorAll('mark.wr-anno-flash');
    for(var i=marks.length-1;i>=0;i--){
      var m=marks[i], p=m.parentNode;
      while(m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
    }
  }
  /* 手动收集 block 下的文本节点（不依赖 document.createTreeWalker，规避 Shadow 节点上的兼容性隐患） */
  function collectTextNodes(root){
    var out=[];
    (function walk(node){
      if(node.nodeType===3){ out.push(node); return; }
      if(node.shadowRoot) return;            /* 不进入嵌套 shadow */
      var c=node.childNodes;
      for(var i=0;i<c.length;i++) walk(c[i]);
    })(root);
    return out;
  }
  /* 在 block 内精确包裹 selectedText 为 <mark class="wr-anno-flash">；跨节点时退回整块闪烁 */
  function wrapSnippet(block, text){
    text=(text||'').replace(/\s+/g,' ').trim();
    if(!text) return null;
    var nodes=collectTextNodes(block);
    if(!nodes.length) return null;
    var full=nodes.map(function(x){return x.nodeValue;}).join('');
    var idx=full.indexOf(text);
    if(idx<0){ text=text.slice(0,Math.max(8,Math.floor(text.length/2))); idx=full.indexOf(text); }
    if(idx<0) return null;
    var end=idx+text.length, acc=0, sNode=null, sOff=0, eNode=null, eOff=0;
    for(var i=0;i<nodes.length;i++){
      var len=nodes[i].nodeValue.length;
      if(sNode===null && acc+len>idx){ sNode=nodes[i]; sOff=idx-acc; }
      if(acc+len>=end){ eNode=nodes[i]; eOff=end-acc; break; }
      acc+=len;
    }
    if(!sNode||!eNode) return null;
    var range=document.createRange();
    try{ range.setStart(sNode,sOff); range.setEnd(eNode,eOff); }catch(e){ return null; }
    var mk=document.createElement('mark');
    mk.className='wr-anno-flash';
    try{ range.surroundContents(mk); }
    catch(e){
      try{ var frag=range.extractContents(); mk.appendChild(frag); range.insertNode(mk); }
      catch(e2){ return null; }
    }
    return mk;
  }
  function flashAnno(anno){
    ensureShadowCss();   /* 关键：切章 build() 可能刚清空 Shadow 内高亮样式，此处同步补回，mark 才有 CSS 显形 */
    var block=locateEl(anno);
    if(!block){ var sec=getSR()&&getSR().querySelector('.wr-secs .wr-sec'); block=sec; }  /* 兜底：定位不到具体块就高亮整节 */
    if(!block) return;
    unwrapFlash();
    block.scrollIntoView({block:'center'});
    var mk=wrapSnippet(block, anno.selectedText);
    if(!mk){ block.classList.add('wr-anno-flash-block'); setTimeout(function(){block.classList.remove('wr-anno-flash-block');},2700); }
  }

  /* ---- 列表 / 导出 ---- */
  function esc(s){return (''+s).replace(/[&<>"]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function updateCount(){if(bList)bList.innerHTML=SVG_LIST+' 批注('+load().length+')';}
  function findAnno(id){var arr=load(); for(var i=0;i<arr.length;i++){ if(arr[i].id===id) return arr[i]; } return null;}
  /* 编辑：复用弹窗，预填备注+类型；保存时 annoSave 走「更新」分支（不再新建） */
  function startEdit(anno){
    if(!anno)return;
    editingId=anno.id; pendingCtx=null;
    var pop=document.getElementById('annoPop');
    document.getElementById('annoText').value=anno.comment||'';
    setType(anno.type||'文字');
    document.getElementById('annoSave').textContent='更新';
    pop.style.display='block';
    pop.style.left=Math.max(8,(window.innerWidth-300)/2)+'px';
    pop.style.top=Math.max(8,(window.innerHeight-220)/2)+'px';
    document.getElementById('annoText').focus();
  }
  /* 删除：直接删（无确认），同步撤掉左侧常驻标记与计数 */
  function deleteAnno(id){
    var annos=load().filter(function(a){return a.id!==id;});
    save(annos); ensureShadowCss(); applyHighlights(); renderList(); updateCount();
  }
  function renderList(){
    var list=document.getElementById('annoList'); var annos=load();
    if(!annos.length){list.innerHTML='<div class="anno-empty">暂无批注。<br>打开「标注」，在正文选中文字/图片即可批注。</div>'; updateCount(); return;}
    var groups={}; annos.forEach(function(a){(groups[a.file]=groups[a.file]||[]).push(a);});
    var html='';
    Object.keys(groups).forEach(function(f){
      html+='<div class="anno-grp"><h4>'+esc(f)+' · '+(groups[f][0].chapterTitle||'')+'</h4>';
      groups[f].forEach(function(a){
        html+='<div class="anno-item" data-id="'+esc(a.id||'')+'">'
          +'<span class="badge">'+esc(a.type||'')+'</span>'
          +'<span class="snip">'+esc(a.snippet||'(图/版式)')+'</span>'
          +(a.comment?'<div class="cm">'+esc(a.comment)+'</div>':'')
          +'<div class="metaline">'+(a.context&&a.context.nearestHeading?esc(a.context.nearestHeading):'')+'</div>'
          +'<span class="acts"><button class="act-edit" title="编辑" aria-label="编辑">'+SVG_EDIT+'</button><button class="act-del" title="删除" aria-label="删除">'+SVG_DEL+'</button></span>'
          +'</div>';
      });
      html+='</div>';
    });
    list.innerHTML=html;
    list.querySelectorAll('.anno-item').forEach(function(it){
      var id=it.dataset.id;
      it.querySelector('.act-edit').addEventListener('click',function(e){e.stopPropagation(); startEdit(findAnno(id));});
      it.querySelector('.act-del').addEventListener('click',function(e){e.stopPropagation(); deleteAnno(id);});
      it.onclick=function(){
        var anno=findAnno(id); if(!anno)return;
        ensureShadowCss();
        var curSec=getSR()&&getSR().querySelector('.wr-secs .wr-sec');
        if(anno.chapterIdx!=null && curSec && chapterIdxOf(curSec)!==anno.chapterIdx){
          goChapter(anno.chapterIdx); applyHighlights(); /* 切章后即时补回常驻标记 */
        }
        flashAnno(anno);
      };
    });
    updateCount();
  }
  function download(name,content,type){
    var blob=new Blob([content],{type:type}); var url=URL.createObjectURL(blob);
    var a=document.createElement('a'); a.href=url; a.download=name; document.body.appendChild(a); a.click();
    setTimeout(function(){URL.revokeObjectURL(url); a.remove();},120);
  }
  document.getElementById('annoClose').onclick=function(){document.getElementById('annoPanel').classList.remove('open'); syncBackdrop();};
  /* 窄屏半透明蒙层：展开批注面板时显示，点击蒙层关闭（与目录抽屉一致） */
  var annoBd=document.getElementById('annoBackdrop');
  if(annoBd){ annoBd.onclick=function(){document.getElementById('annoPanel').classList.remove('open'); syncBackdrop();}; }
  function syncBackdrop(){
    var p=document.getElementById('annoPanel'); if(!p) return;
    var show=p.classList.contains('open') && window.matchMedia('(max-width: 900px)').matches;
    if(annoBd) annoBd.style.display = show ? 'block' : 'none';
  }
  window.addEventListener('resize', syncBackdrop);
  document.getElementById('annoExportJson').onclick=function(){download('annotations.json',JSON.stringify(load(),null,2),'application/json');};
  document.getElementById('annoExportMd').onclick=function(){
    var annos=load(); var groups={}; annos.forEach(function(a){(groups[a.file]=groups[a.file]||[]).push(a);});
    var md='# 校对批注任务清单（微信读书本地预览）\n\n> 由预览页「选中即批注」导出。每条含源 xhtml 路径、锚点（文字片段/最近标题）、严重级与备注。\n\n';
    Object.keys(groups).forEach(function(f){
      md+='## '+f+' · '+(groups[f][0].chapterTitle||'')+'\n\n';
      groups[f].forEach(function(a){
        md+='- **'+(a.type||'')+'**：「'+(a.snippet||'(图/版式)')+'」→ '+(a.comment||'(未填备注)')+'\n';
        if(a.context&&a.context.nearestHeading)md+='  - 上下文：'+a.context.nearestHeading+'\n';
      });
      md+='\n';
    });
    download('annotations_for_agent.md',md,'text/markdown');
  };
  document.getElementById('annoClear').onclick=function(){if(confirm('清空本书全部批注？')){STORE.set(LS_KEY,'[]');applyHighlights();renderList();}};

  /* ---- 初始化（等引擎把内容挂进 shadow） ---- */
  function start(){var s=getSR(); if(s&&s.querySelector('.wr-secs')){ensureShadowCss();applyHighlights();renderList();}else setTimeout(start,80);}
  if(document.readyState==='complete'||document.readyState==='interactive')start(); else document.addEventListener('DOMContentLoaded',start);
})();
</script>
<!-- /wr-anno:js -->
"""


def strip_markers(html: str) -> str:
    html = re.sub(rf"{CSS_BEGIN}.*?{CSS_END}", "", html, flags=re.S)
    html = re.sub(rf"{HTML_BEGIN}.*?{HTML_END}", "", html, flags=re.S)
    html = re.sub(rf"{JS_BEGIN}.*?{JS_END}", "", html, flags=re.S)
    return html


def inject(html: str) -> str:
    html = strip_markers(html)
    html = html.replace("</head>", CSS + "\n</head>", 1)
    html = html.replace("</body>", HTML + "\n" + JS + "\n</body>", 1)
    return html


def main(argv=None):
    ap = argparse.ArgumentParser(description="给 weread 预览页注入选中即批注层")
    ap.add_argument("html", help="预览页 index.html 路径")
    ap.add_argument("--undo", action="store_true", help="移除已注入的批注层")
    args = ap.parse_args(argv)

    path = Path(args.html)
    if not path.is_file():
        print(f"找不到文件: {path}", file=sys.stderr)
        return 2

    raw = path.read_text(encoding="utf-8")
    already = CSS_BEGIN in raw

    if args.undo:
        if not already:
            print("未检测到注入标记，无需移除。")
            return 0
        out = strip_markers(raw)
        path.write_text(out, encoding="utf-8")
        print(f"已移除批注层：{path}")
        return 0

    if already:
        print("检测到已有注入标记，将先移除再重新注入（幂等）。")
    out = inject(raw)
    path.write_text(out, encoding="utf-8")
    print(f"已注入批注层：{path}")
    print("使用说明：打开预览页 → 点「✏️ 标注」→ 选中正文 → 填备注保存；点「📋 批注」导出 JSON / MD。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
