'use client';

import { useCallback, useEffect, useState } from 'react';
import { Activity, ArrowDownToLine, ArrowRight, Database, KeyRound, Pause, RefreshCw, ShieldCheck, ChevronDown, Check, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table';

type History = { n:number; wins:number; losses:number; winRate:number|null; complete:boolean; missing:number; championGames:number; roleGames:number|null; streak:number; matchIds:string[] };
type Player = { puuid:string; name:string; tag:string; champion:string; role:string; level:number|null; team:number; isSelf:boolean; history:History|null; rankSnapshot:{observedAt:number; rank:{tier:string;rank:string;leaguePoints:number}|null}|null };
type Match = { id:string; startedAt:number; duration:number; win:boolean; champion:string; team:number; participants:Player[]; complete:boolean; allyMean:number|null; enemyMean:number|null; gap:number|null; historiesReady:number };
type Status = { csrf:string; connected:boolean; snapshotCount:number; cachedMatches:number; matches:Match[]; job:{status:string;message:string;done:number;total:number;requests:number;finishedAt?:number;warning?:string;anchorGaps?:number} };
type ModelContext = {registerTool:(tool:{name:string;description:string;inputSchema:object;annotations:object;execute:(input:unknown)=>Promise<unknown>},options:{signal:AbortSignal})=>void|Promise<void>};
const date = (ms:number) => new Date(ms).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const number = (n:number|null, suffix='') => n===null?'—':`${n.toFixed(1)}${suffix}`;

export default function Home() {
  const [data,setData] = useState<Status|null>(null);
  const [key,setKey] = useState('');
  const [error,setError] = useState('');
  const [online,setOnline] = useState(false);
  const [pending,setPending] = useState(false);
  const [selected,setSelected] = useState<string|null>(null);
  const [showKey,setShowKey] = useState(false);
  const [exporting,setExporting] = useState(false);
  const load = useCallback(async()=>{
    const response = await fetch('/api/status',{cache:'no-store'});
    if(!response.ok) throw new Error('Collector unavailable');
    const value:Status = await response.json();
    setData(value);setOnline(true);
    return value;
  },[]);
  useEffect(()=>{
    let mounted=true;
    const poll=async()=>{try{if(mounted)await load();}catch{if(mounted)setOnline(false);}};
    void poll();const timer=setInterval(()=>void poll(),4000);
    return()=>{mounted=false;clearInterval(timer);};
  },[load]);
  const action=useCallback(async(path:string,body:object={})=>{
    setPending(true);setError('');
    try{
      const current=await load();
      const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Queue-Lab-Token':current.csrf},body:JSON.stringify(body)});
      const value=await response.json() as {error?:string};
      if(!response.ok)throw new Error(value.error||'The request failed.');
      setKey('');setShowKey(false);await load();
    }catch(e){setError(e instanceof Error?e.message:'Cannot reach the local collector.');throw e;}
    finally{setPending(false);}
  },[load]);
  useEffect(()=>{
    const context=(document as Document & {modelContext?:ModelContext}).modelContext;
    if(!context?.registerTool)return;
    const lifecycle=new AbortController();
    const tools=[
      {name:'read_queue_lab_status',description:'Read current import progress and saved matchmaking comparisons. Does not reveal the Riot key.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute:async(input:unknown)=>{if(!input||typeof input!=='object'||Object.keys(input).length)throw new Error('Expected an empty object.');const {csrf,...value}=await load();void csrf;return value;}},
      {name:'refresh_queue_lab_profile',description:'Start or resume the local Riot import for Llewellyn#300 using the key already entered by the user. Retrieves new matches and rank snapshots.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:true},execute:async(input:unknown)=>{if(!input||typeof input!=='object'||Object.keys(input).length)throw new Error('Expected an empty object.');await action('/api/import');return {started:true};}},
    ];
    for(const tool of tools){try{void Promise.resolve(context.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{});}catch{}}
    return()=>lifecycle.abort();
  },[action,load]);
  const matches=data?.matches??[];
  const complete=matches.filter(m=>m.complete);
  const avg=complete.length?complete.reduce((n,m)=>n+(m.gap??0),0)/complete.length:null;
  const wins=matches.filter(m=>m.win).length;
  const running=data?.job.status==='running'||data?.job.status==='pausing';
  const active=matches.find(m=>m.id===selected)??null;
  const connect=!data?.connected||showKey;
  const handleAction=(path:string,body:object={})=>{void action(path,body).catch(()=>{});};
  const exportData=async()=>{
    setExporting(true);setError('');
    try{const response=await fetch('/api/export');if(!response.ok)throw new Error('Export failed.');const value=await response.json();const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='queue-lab-observations.json';a.click();URL.revokeObjectURL(url);}catch{setError('Could not export observations. Check the collector and retry.');}finally{setExporting(false);}
  };
  return <div className="shell">
    <header className="topbar"><a className="wordmark" href="/"><Activity size={23} /> QUEUE LAB</a><span className="local-badge"><span className={online?'':'offline'}/>{online?'Local collector connected':'Local collector offline'}</span></header>
    <main className="workspace">
      <div className="page-heading"><div><p className="eyebrow">MATCHMAKING / PERSONAL PILOT</p><h1>Your matches, in context.</h1><p className="subtitle">Compare the histories players brought into each game.</p></div><span className="version">PILOT 01</span></div>
      <section className="account-strip"><div className="account-mark">L</div><div><h2>Llewellyn<span>#300</span></h2><p>North America <span className="dot">·</span> Ranked Solo / Duo</p></div><Button className="refresh" disabled={!online||!data?.connected||pending||running} onClick={()=>handleAction('/api/import')}><RefreshCw size={16}/> {data?.job.status==='paused'?'Resume import':'Refresh profile'}</Button></section>
      <section className="stats-grid"><div><p>Matches in this pilot</p><strong>{matches.length} <small>/ 20</small></strong><span>{matches.length?`${wins} wins · ${matches.length-wins} losses`:'Completed ranked games'}</span></div><div><p>Histories processed</p><strong>{data?.job.done??0} <small>/ {data?.job.total||200}</small></strong><span>{complete.length} complete team comparisons</span></div><div><p>Team history difference</p><strong>{avg===null?'—':`${avg>0?'+':''}${avg.toFixed(1)}`} <small>{avg===null?'':'pp'}</small></strong><span>Teammates minus opponents</span></div><div><p>Rank snapshots</p><strong>{data?.snapshotCount??0}</strong><span>At observation time</span></div></section>
      {!online&&<div className="notice"><AlertCircle size={18}/><div>The collector is not responding. Start Queue Lab’s local launcher; the page will reconnect automatically.</div></div>}
      {error&&<div className="notice error" role="alert"><AlertCircle size={18}/>{error}</div>}
      {connect&&<section className="connect-panel"><div className="connect-copy"><div className="icon-disc"><KeyRound/></div><p className="eyebrow">CONNECT YOUR DATA</p><h2>Start with the games<br/>you’ve already played.</h2><p>A Riot development key retrieves your last 20 ranked games and each player’s 20 earlier games. First import: allow roughly 60–90 minutes; overlap can shorten it.</p><a href="https://developer.riotgames.com/" target="_blank" rel="noreferrer">Get a key from Riot <ArrowRight size={15}/></a></div><form className="connect-form" onSubmit={e=>{e.preventDefault();handleAction('/api/import',{key});}}><label htmlFor="riot-key">Riot API key</label><Input id="riot-key" type="password" value={key} onChange={e=>setKey(e.target.value)} autoComplete="off" placeholder="RGAPI-…" disabled={running||pending} required/><p><ShieldCheck size={15}/> Kept in memory on this computer. Never saved to disk.</p><Button type="submit" disabled={!online||pending||running||!key.trim()}>{pending?'Connecting…':matches.length?'Connect & resume import':'Connect & import 20 matches'}<ArrowRight/></Button><small>Development keys expire after 24 hours. Saved matches stay available.</small></form></section>}
      {data&&data.job.status!=='idle'&&<section className="job" aria-live="polite"><div className="job-copy">{running?<RefreshCw size={19} className="spin"/>:data.job.status==='complete'?<Check size={19}/>:<Database size={19}/>}<div><strong>{data.job.message}</strong><p>{data.cachedMatches.toLocaleString()} saved matches · {data.job.requests.toLocaleString()} requests this run{data.job.finishedAt&&data.job.status==='complete'?` · ${date(data.job.finishedAt)}`:''}</p></div></div><div className="job-actions">{running?<Button variant="outline" disabled={pending||data.job.status==='pausing'} onClick={()=>handleAction('/api/pause')}><Pause/>Pause</Button>:data.connected&&<Button variant="ghost" onClick={()=>setShowKey(!showKey)}>Replace key</Button>}</div>{!!data.job.warning&&<p className="warning">{data.job.warning}</p>}{!!data.job.anchorGaps&&<p className="warning">{data.job.anchorGaps} recent match records were unavailable. This sample may have gaps.</p>}</section>}
      <div className="section-heading"><div><p className="eyebrow">THE EVIDENCE</p><h2>Match history</h2></div><Button variant="outline" disabled={!matches.length||!online||exporting} onClick={()=>void exportData()}><ArrowDownToLine/>Export observations</Button></div>
      {!matches.length?<section className="empty"><Database size={26}/><h3>Your first comparison will appear here.</h3><p>Four teammates. Five opponents. Only the games they finished before yours.</p><div className="method-tags"><span>Same queue</span><span>Chronological histories</span><span>Missing data stays visible</span></div></section>:<section className="match-list"><Table><TableHeader><TableRow><TableHead>Match</TableHead><TableHead>Your champion</TableHead><TableHead>Teammates</TableHead><TableHead>Opponents</TableHead><TableHead>Difference</TableHead><TableHead>Coverage</TableHead><TableHead><span className="sr-only">Details</span></TableHead></TableRow></TableHeader><TableBody>{matches.map(m=><TableRow key={m.id} data-state={active?.id===m.id?'selected':undefined}><TableCell><span className={`result ${m.win?'win':'loss'}`}>{m.win?'Win':'Loss'}</span><small className="match-date">{date(m.startedAt)}</small></TableCell><TableCell>{m.champion}<small className="match-date">{Math.floor(m.duration/60)}m {Math.floor(m.duration%60)}s</small></TableCell><TableCell>{number(m.allyMean,'%')}</TableCell><TableCell>{number(m.enemyMean,'%')}</TableCell><TableCell><span className={m.gap!==null&&m.gap<0?'negative':'positive'}>{m.gap===null?'—':`${m.gap>0?'+':''}${m.gap.toFixed(1)} pp`}</span></TableCell><TableCell><span className={`coverage ${m.complete?'ready':''}`}>{m.complete?'Complete':`${m.historiesReady}/10 processed`}</span></TableCell><TableCell><Button variant="ghost" size="sm" aria-expanded={active?.id===m.id} aria-label={`View ${m.champion} match from ${date(m.startedAt)}`} onClick={()=>setSelected(active?.id===m.id?null:m.id)}>Details<ChevronDown size={14}/></Button></TableCell></TableRow>)}</TableBody></Table></section>}
      {!!matches.length&&<p className="table-note">Percentages are averages of players’ prior win rates, not predictions. A difference is shown only when all nine other players have complete histories.</p>}
      {active&&<section className="details" aria-label="Selected match details"><div className="section-heading"><div><p className="eyebrow">{active.id}</p><h2>{active.champion} · {date(active.startedAt)}</h2></div><Button variant="ghost" onClick={()=>setSelected(null)}>Close details</Button></div><div className="team-grid">{[true,false].map(ally=><div key={String(ally)}><h3>{ally?'Your team':'Opponents'}</h3>{active.participants.filter(p=>(p.team===active.team)===ally).map(p=><PlayerCard key={p.puuid} player={p}/>)}</div>)}</div></section>}
      <section className="methodology"><h3>How to read this pilot</h3><p>A positive difference means your four teammates had a higher average recent win rate than the five opponents. You are excluded from that comparison. This does not measure skill or the chance of winning.</p><p>Histories use Ranked Solo/Duo games completed before the shared match. Games shorter than three minutes are excluded as a remake approximation. Rank snapshots are current when fetched; account levels come from match records. Role familiarity means prior games in that role, not confirmed autofill.</p></section>
      <footer><p>Twenty matches are an exploratory sample. Overlapping histories and repeat players make observations dependent. This tool cannot establish matchmaking intent, internal MMR, or whether an account is a smurf.</p><p>Queue Lab is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.</p></footer>
    </main></div>;
}

function PlayerCard({player:p}:{player:Player}){
  const h=p.history;const rank=p.rankSnapshot?.rank;
  return <article className={`player ${p.isSelf?'self':''}`}><div className="player-heading"><div><strong>{p.name}<span>#{p.tag}{p.isSelf?' · you':''}</span></strong><p>{p.champion} · {p.role||'Role unavailable'}</p></div><b>{number(h?.winRate??null,'%')}</b></div><div className="player-stats"><span>{h?`${h.wins}W / ${h.losses}L · ${h.n}/20 games`:'History pending'}</span><span>Account level {p.level??'—'}</span></div>{h&&<><div className="history-bar" aria-label={`${h.wins} wins in ${h.n} prior games`}><span style={{width:`${h.winRate??0}%`}}/></div><div className="player-stats"><span>Champion: {h.championGames}/{h.n}</span><span>Role: {h.roleGames??'—'}/{h.n}</span><span>{h.streak>0?`${h.streak}W streak`:h.streak<0?`${-h.streak}L streak`:'—'}</span></div>{!h.complete&&<p className="warning">Partial history{h.missing?` · ${h.missing} unavailable records`:''}; excluded from team comparison.</p>}</>}<p className="rank-note">{p.rankSnapshot?`${rank?`${rank.tier} ${rank.rank} · ${rank.leaguePoints} LP`:'Unranked'} · observed ${date(p.rankSnapshot.observedAt)}`:'No rank snapshot yet'}</p></article>;
}
