import React, {useEffect, useState} from 'react';
export function DesignIcon({name,size=20}) {
 const paths={sun:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1.5 1.5 M17.5 17.5L19 19 M5 19l1.5-1.5 M17.5 6.5L19 5',moon:'M20 15.5A9 9 0 0 1 8.5 4 9 9 0 1 0 20 15.5Z',file:'M14 3H5v18h14V8z M14 3v5h5 M8 12h8 M8 16h6',shield:'M12 3l8 4v5c0 5-8 9-8 9s-8-4-8-9V7z M8 12l3 3 5-6',check:'M5 12l4 4L19 6',bell:'M6 10a6 6 0 0 1 12 0v6l2 2H4l2-2z M10 21h4'};
 return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]||paths.file}/></svg>;
}
export function ThemeToggle(){
 const [theme,setTheme]=useState(()=>document.documentElement.dataset.theme||'light');
 useEffect(()=>{const media=matchMedia('(prefers-color-scheme: dark)');const sync=()=>setTheme(document.documentElement.dataset.theme);const system=()=>{let saved;try{saved=localStorage.getItem('anumati-theme')}catch{}if(!saved){document.documentElement.dataset.theme=media.matches?'dark':'light';sync()}};const storage=e=>{if(e.key==='anumati-theme'){document.documentElement.dataset.theme=e.newValue==='dark'?'dark':e.newValue==='light'?'light':media.matches?'dark':'light';sync()}};window.addEventListener('anumati-theme-change',sync);window.addEventListener('storage',storage);media.addEventListener('change',system);return()=>{window.removeEventListener('anumati-theme-change',sync);window.removeEventListener('storage',storage);media.removeEventListener('change',system)}},[]);
 return <button className="theme-toggle" type="button" aria-label={'Switch to '+(theme==='dark'?'light':'dark')+' theme'} title={'Switch to '+(theme==='dark'?'light':'dark')+' theme'} onClick={()=>{const next=theme==='dark'?'light':'dark';document.documentElement.dataset.theme=next;try{localStorage.setItem('anumati-theme',next)}catch{}window.dispatchEvent(new Event('anumati-theme-change'));setTheme(next)}}><DesignIcon name={theme==='dark'?'sun':'moon'}/></button>;
}
export function UtilityBar({institution=false}){
 const [size,setSize]=useState(()=>Number(document.documentElement.style.getPropertyValue('--font-scale'))||1);
 const resize=n=>{setSize(n);document.documentElement.style.setProperty('--font-scale',n)};
 return <div className="utility"><span><i className="tricolour"/>Anumati · {institution?'Institution services':'Technical education approval workspace'}</span><div className="utility-controls"><span className="language-label">English</span><button type="button" aria-label="Decrease text size" onClick={()=>resize(Math.max(.9,size-.05))}>A−</button><button type="button" aria-label="Reset text size" onClick={()=>resize(1)}>A</button><button type="button" aria-label="Increase text size" onClick={()=>resize(Math.min(1.25,size+.05))}>A+</button><ThemeToggle/><span className="prototype-label">SIH 26262 · Prototype</span></div></div>;
}
export function StoryDecoration(){return <><div className="floating-shapes" aria-hidden="true"><i/><i/><i/><i/></div></>}
export function WorkflowGraphic(){return <div className="workflow-graphic" aria-hidden="true"><span><DesignIcon name="file"/></span><i/><span><DesignIcon name="shield"/></span><i/><span><DesignIcon name="check"/></span></div>}
