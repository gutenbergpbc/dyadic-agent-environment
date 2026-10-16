const escapePanel=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function renderMethodPanels(container, results, model, detail, katex) {
 const B=results.behaviors,E=escapePanel,doses=['none','low','medium','high'];
 const figure=container.closest('.figure');
 let control=figure.querySelector('#prevalence-scale');
 if(!control){const wrap=document.createElement('div');wrap.className='controls';wrap.innerHTML='<label>Y-axis<select id="prevalence-scale"><option value="detail">Detail · fit collected data</option><option value="shared">Shared · 0–100%</option></select></label>';container.before(wrap);control=wrap.querySelector('select');}
 control.onchange=()=>renderMethodPanels(container,results,model,detail,katex);
 let tooltip=figure.querySelector('.value-tooltip');
 if(!tooltip){tooltip=document.createElement('div');tooltip.className='value-tooltip';tooltip.id='prevalence-value-tooltip';tooltip.role='tooltip';tooltip.hidden=true;figure.append(tooltip);}
 let active=null;const hide=()=>{tooltip.hidden=true;if(active)active.removeAttribute('aria-describedby');active=null;};hide();
 const show=el=>{hide();active=el;el.setAttribute('aria-describedby',tooltip.id);tooltip.innerHTML=el.dataset.compact;tooltip.hidden=false;const box=el.getBoundingClientRect();tooltip.style.left=Math.max(8,Math.min(innerWidth-tooltip.offsetWidth-8,box.x+box.width/2-tooltip.offsetWidth/2))+'px';tooltip.style.top=Math.max(8,Math.min(innerHeight-tooltip.offsetHeight-8,box.top-tooltip.offsetHeight-8))+'px';};
 // Register viewport listeners once, with a handler replaced on each redraw.
 container._hideValueTooltip=hide;
 if(!container._valueListeners){container._valueListeners=true;window.addEventListener('scroll',()=>container._hideValueTooltip(),true);window.addEventListener('resize',()=>container._hideValueTooltip());document.addEventListener('keydown',e=>{if(e.key==='Escape')container._hideValueTooltip()});}

 const tex=x=>katex.renderToString(x,{throwOnError:false});
 const legend=container.closest('.figure').querySelector('.legend');
 legend.innerHTML=B.map((b,i)=>`<button class="behavior-key" data-b="${i}">${tex(`B_${b.number}`)} ${E(b.label)}</button>`).join('');
 legend.querySelectorAll('[data-b]').forEach(el=>el.onclick=()=>{const b=B[+el.dataset.b];detail(b.label,b.definition+' '+b.interpretation)});
 container.innerHTML=results.comparison_methods.map(m=>{
  const nr=results.runs.filter(r=>r.method===m.id&&r.model===model&&r.status==='provisional').length; const method=results.methods.find(x=>x.id===m.id),W=420,H=285,L=36,R=414,T=12,BT=223,g=(R-L)/4,bw=17,gap=3;let svg='';
  const rows=results.data.filter(r=>r.view==='prevalence'&&r.dimensions.method===m.id&&r.dimensions.model===model&&B.some(b=>b.id===r.dimensions.behavior));
  const allMeasured=rows.length>0&&rows.every(r=>!!r.status),largest=Math.max(0,...rows.map(r=>r.status?(r.value??0):(r.mock_value??0)));
  const ceiling=control.value==='detail'&&allMeasured?Math.min(100,Math.max(25,Math.ceil(largest*100/5)*5)):100,step=ceiling<=25?5:25;
  const ticks=Array.from({length:Math.floor(ceiling/step)+1},(_,i)=>i*step);if(ticks.at(-1)!==ceiling)ticks.push(ceiling);

  for(const v of ticks){const y=BT-v/ceiling*(BT-T);svg+=`<path class="grid" d="M${L} ${y}H${R}"/><text x="${L-8}" y="${y+4}" text-anchor="end">${v}</text>`}
  doses.forEach((dose,di)=>{const center=L+g*(di+.5),d=method.doses.find(x=>x.id===dose),tip=d.tooltip||'Dose definition pending.';
   svg+=`<text class="dose-label" tabindex="0" role="button" data-dose-detail="${E(tip)}" x="${center}" y="${BT+37}" text-anchor="middle"><title>${E(tip)}</title>${dose[0].toUpperCase()+dose.slice(1)}</text>`;
   B.forEach((b,i)=>{const row=results.data.find(r=>r.view==='prevalence'&&r.dimensions.method===m.id&&r.dimensions.model===model&&r.dimensions.dose===dose&&r.dimensions.behavior===b.id);if(!row)return;
    const real=!!row.status,v=real?(row.tooltip.lower??row.value):row.mock_value,x=center-(3*bw+2*gap)/2+i*(bw+gap),y=v==null?BT:BT-v*100/ceiling*(BT-T),t=row.tooltip;
    const top=y,percent=v==null?'Not estimated':(v*100).toFixed(1)+'%';
    const body=`${m.name} · ${dose}\n${tip}\n${b.label}\n${v==null?'Not yet analyzed':`${t.detected_dyads??t.numerator}/${real?t.assigned:t.denominator} detected dyads; ${percent}`}\n${real?`${t.assigned} collected; ${t.unknown} unknown; ${t.execution_errors} execution errors in this cell.\nAI-labeled; manual review pending. Not detected is provisional, not verified absence.`:'Mock synthetic values; no measured result.'}\n${t.reason||''}\nUnresolved cases are disclosed above, not classified as absent. No sampling confidence interval is estimated. Missed behaviors remain possible. Repeated events count once. Categories may overlap.\n${b.interpretation||''}${t.positive_episode_ids?.length?'\nPositive dyads: '+t.positive_episode_ids.join(', '):''}`;
    const compact=`<strong>${v==null?'Not estimated':percent}</strong> · ${v==null?(t.known_occurrences||0)+' known events':(t.detected_dyads??t.numerator)+'/'+(real?t.assigned:t.denominator)+' dyads'+(real?'*':' · mock')}<br><span>${E(dose[0].toUpperCase()+dose.slice(1))} · B${b.number}${real&&t.unknown?' · '+t.unknown+' unknown':''}</span>`;
    svg+=`<g class="prevalence-mark" role="button" tabindex="0" aria-label="${E(body)}" data-datum="${E(body)}" data-compact="${E(compact)}"><rect class="mark" x="${x}" y="${v===0?BT-1:y}" width="${bw}" height="${v==null?0:Math.max(2,BT-y)}" fill="${real?'var(--real)':'var(--purple)'}"/><rect x="${x-1}" y="${Math.min(top,y,BT-14)}" width="${bw+2}" height="${Math.max(14,BT-Math.min(top,y))+3}" fill="transparent"/></g><text x="${x+bw/2}" y="${BT+15}" text-anchor="middle" style="font-size:10px">B${b.number}</text>`;
    if(v==null)svg+=`<text x="${x+bw/2}" y="${BT-5}" text-anchor="middle">—</text>`;
   });
  });
  return `<article class="panel"><h3>${tex(m.tex)} ${E(m.name)}</h3><p class="unit">Dyads containing behavior (%) · <b>0–${ceiling}%</b>${nr?` · ${nr} collected`:""}</p><svg viewBox="0 0 ${W} ${H}" role="group" aria-label="${E(m.name)} behavior prevalence">${svg}</svg><p class="dose-axis">Pressure dose · hover or select a label</p></article>`;
 }).join('');
 container.querySelectorAll('[data-datum],[data-dose-detail]').forEach(el=>{const open=()=>{hide();detail(el.dataset.datum?'Behavior prevalence':'Exact dose',el.dataset.datum||el.dataset.doseDetail)};if(el.dataset.compact){el.onpointerenter=()=>show(el);el.onpointerleave=hide;el.onfocus=()=>show(el);el.onblur=hide;}el.onclick=open;el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open()}}});
}
