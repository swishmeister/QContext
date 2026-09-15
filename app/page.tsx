'use client';

import { Fragment, useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ArrowDownToLine, Database, Pause, RefreshCw, ChevronDown, Check, AlertCircle, Search, Users } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { profileStats } from '@/lib/profile-stats';
import championIcons from '@/lib/champion-icons.json';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table';

type History = { n:number; wins:number; losses:number; winRate:number|null; complete:boolean; missing:number; championGames:number; championHabit?:'unknown'|'first_in_20'|'one_trick'|'main'|'flex'; championCounts?:Record<string,number>; roleGames:number|null; streak:number; matchIds:string[]; mainRoles?:string[]; mainRoleGames?:number; roleStatus?:string; roleCounts?:Record<string,number> };
type Player = { puuid:string; name:string; tag:string; champion:string; championId?:number; profileIconId?:number|null; role:string; level:number|null; team:number; isSelf:boolean; history:History|null; rankSnapshot:{observedAt:number; rank:{tier:string;rank:string;leaguePoints:number}|null}|null };
type DuoPair = {players:string[];status:'duo'|'not_duo'|'possible';source:'manual'|'history';sharedGames:number;matchIds:string[];historyComplete:boolean;recordedAt:number|null};
type RankAverage = {label:string|null;count:number;total:number;oldestObservation:number|null;newestObservation:number|null};
type Match = { id:string; startedAt:number; duration:number; win:boolean; champion:string; team:number; participants:Player[]; complete:boolean; allyMean:number|null; enemyMean:number|null; gap:number|null; historiesReady:number; duoPairs?:DuoPair[]; teamRanks?:{allies:RankAverage;enemies:RankAverage} };
type Profile = {name:string;tag:string;platform:string;puuid?:string;iconUrl?:string|null;level?:number|null};
type Status = {csrf:string; account:Profile; connected:boolean; snapshotCount:number; cachedMatches:number; matches:Match[]; job:{status:string;message:string;done:number;total:number;requests:number;finishedAt?:number;warning?:string;anchorGaps?:number} };
type ModelContext = {registerTool:(tool:{name:string;description:string;inputSchema:object;annotations:object;execute:(input:unknown)=>Promise<unknown>},options:{signal:AbortSignal})=>void|Promise<void>};
const date = (ms:number) => new Date(ms).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const number = (n:number|null, suffix='') => n===null?'—':`${n.toFixed(1)}${suffix}`;
const roleName = (role:string) => ({TOP:'Top',JUNGLE:'Jungle',MIDDLE:'Mid',BOTTOM:'ADC',UTILITY:'Support',SUPPORT:'Support'}[role]||'Unknown role');

export default function Home() {
  const [data,setData] = useState<Status|null>(null);
  const [error,setError] = useState('');
  const [online,setOnline] = useState(false);
  const [pending,setPending] = useState(false);
  const [selected,setSelected] = useState<string|null>(null);
  const [exporting,setExporting] = useState(false);
  const [search,setSearch] = useState('Llewellyn#300');
  const statusRequest = useRef<Promise<Status>|null>(null);
  const profile=data?.account??{name:'Llewellyn',tag:'300',platform:'NA'};
  useEffect(()=>{if(data?.account){setSearch(`${data.account.name}#${data.account.tag}`);setSelected(null);}},[data?.account?.puuid]);
  const load = useCallback(()=>{
    if(statusRequest.current)return statusRequest.current;
    const request=(async()=>{
      const response = await fetch('/api/status',{cache:'no-store',signal:AbortSignal.timeout(35000)});
      if(!response.ok) throw new Error('Collector unavailable');
      const value:Status = await response.json();
      setData(value);setOnline(true);
      return value;
    })();
    statusRequest.current=request.finally(()=>{statusRequest.current=null;});
    return statusRequest.current;
  },[]);
  useEffect(()=>{
    let mounted=true;
    let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try{await load();}catch{if(mounted)setOnline(false);}
      finally{if(mounted)timer=setTimeout(()=>void poll(),4000);}
    };
    void poll();
    return()=>{mounted=false;clearTimeout(timer);};
  },[load]);
  const action=useCallback(async(path:string,body:object={})=>{
    setPending(true);setError('');
    try{
      const current=await load();
      const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Queue-Lab-Token':current.csrf},body:JSON.stringify(body)});
      const value=await response.json() as {error?:string};
      if(!response.ok)throw new Error(value.error||'The request failed.');
      await load();
    }catch(e){setError(e instanceof Error?e.message:'Cannot reach the collector.');throw e;}
    finally{setPending(false);}
  },[load]);
  useEffect(()=>{
    const context=(document as Document & {modelContext?:ModelContext}).modelContext;
    if(!context?.registerTool)return;
    const lifecycle=new AbortController();
    const tools=[
      {name:'read_queue_lab_status',description:'Read current import progress and saved matchmaking comparisons. Does not reveal the Riot key.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true,untrustedContentHint:true},execute:async(input:unknown)=>{if(!input||typeof input!=='object'||Object.keys(input).length)throw new Error('Expected an empty object.');const {csrf,...value}=await load();void csrf;return value;}},
      {name:'refresh_queue_lab_profile',description:'Start or resume the Riot import for the selected profile using the server-configured Riot API key. Retrieves new matches and rank snapshots.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:false,untrustedContentHint:true},execute:async(input:unknown)=>{if(!input||typeof input!=='object'||Object.keys(input).length)throw new Error('Expected an empty object.');await action('/api/import');return {started:true};}},
    ];
    for(const tool of tools){try{void Promise.resolve(context.registerTool(tool,{signal:lifecycle.signal})).catch(()=>{});}catch{}}
    return()=>lifecycle.abort();
  },[action,load]);
  const matches=data?.matches??[];
  const stats=profileStats(matches);
  const running=data?.job.status==='running'||data?.job.status==='pausing';
  const active=matches.find(m=>m.id===selected)??null;
  const handleAction=(path:string,body:object={})=>{void action(path,body).catch(()=>{});};
  const exportData=async()=>{
    setExporting(true);setError('');
    try{const response=await fetch('/api/export');if(!response.ok)throw new Error('Export failed.');const value=await response.json();const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='queue-context-observations.json';a.click();URL.revokeObjectURL(url);}catch{setError('Could not export observations. Check the collector and retry.');}finally{setExporting(false);}
  };
  return <div className="shell">
    <header className="topbar"><a className="wordmark" href="/"><Activity size={23} /> QUEUE CONTEXT</a><div className="topbar-actions"><span className="local-badge"><span className={online?'':'offline'}/>{online?'Collector connected':'Collector offline'}</span>
    </div></header>
    <main className="workspace">
      <div className="page-heading"><div><p className="eyebrow">MATCHMAKING / PERSONAL PILOT</p><h1>Your matches, in context.</h1><p className="subtitle">Compare the histories players brought into each game.</p></div><span className="version">PILOT 01</span></div>
      <form className="profile-search" onSubmit={e=>{e.preventDefault();if(!data?.connected){setError('Riot API access is not configured on the server.');return;}setSelected(null);handleAction('/api/import',{riotId:search});}}><label htmlFor="profile-search">Summoner profile</label><div className="search-controls"><span className="region-label">NA</span><Input id="profile-search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Game name#Tag" required maxLength={80} disabled={running||pending}/><Button type="submit" disabled={!online||!data?.connected||pending||running||!search.trim()}><Search size={16}/>Search profile</Button></div><p>{running?'Pause the current import to search another profile.':'Enter a Riot ID, including the #tag. Saved matches are reused when you switch profiles.'}</p></form>
      <section className="account-strip"><ProfileIcon key={profile.iconUrl} profile={profile}/><div><h2>{profile.name}<span>#{profile.tag}</span></h2><p>North America <span className="dot">·</span> Ranked Solo / Duo{profile.level?` · Level ${profile.level}`:''}</p></div><Button className="refresh" disabled={!online||!data?.connected||pending||running} onClick={()=>handleAction('/api/import')}><RefreshCw size={16}/> {data?.job.status==='paused'?'Resume import':'Refresh profile'}</Button></section>
      <section className="stats-grid" aria-label="Profile statistics">
        <div><p>Current rank</p><RankStat label={stats.currentRank}/><span>{stats.rankObservedAt?`Observed ${date(stats.rankObservedAt)}`:'No rank observation yet'}</span></div>
        <div><p>Win rate · past 20 games</p><strong className="stat-value">{stats.winRate===null?'—':`${stats.winRate.toFixed(1)}%`}</strong><span>{stats.games?`${stats.wins} wins · ${stats.losses} losses${stats.games<20?` · ${stats.games}/20 games available`:''}`:'No completed games yet'}</span></div>
        <div title="Average of all 10 players per match, including you, across the displayed games. Repeat players count once per match. Missing and unranked players are excluded. Uses latest observed ranks, not historical match ranks; 100 LP per division with a shared Master+ LP scale."><p>Lobby average rank</p><RankStat label={stats.lobbyRank}/><span>{stats.games?`Across ${stats.games} games · ${stats.rankedPlayers}/${stats.playerCount} ranks`:'No lobby ranks yet'}</span><span className="stat-note">Latest observed ranks</span></div>
        <div><p>Team history difference</p><strong className="stat-value">{stats.historyDifference===null?'—':`${stats.historyDifference>0?'+':''}${stats.historyDifference.toFixed(1)}`} <small>{stats.historyDifference===null?'':'pp'}</small></strong><span>Teammates minus opponents</span><span className="stat-note">{stats.comparisonCount}/{stats.games} complete comparisons</span></div>
      </section>
      {!online&&<div className="notice"><AlertCircle size={18}/><div>The collector is not responding. Check that Queue Context is running; the page will reconnect automatically.</div></div>}
      {online&&data&&!data.connected&&<div className="notice error" role="alert"><AlertCircle size={18}/><div>Riot API access is not configured. Set RIOT_API_KEY on the server and redeploy.</div></div>}
      {error&&<div className="notice error" role="alert"><AlertCircle size={18}/>{error}</div>}
      {data?.job.status==='error'&&<div className="notice error" role="alert"><AlertCircle size={18}/><div><strong>Import stopped.</strong> {data.job.message}</div></div>}
      {data&&data.job.status!=='idle'&&<section className="job" aria-live="polite"><div className="job-copy">{running?<RefreshCw size={19} className="spin"/>:data.job.status==='complete'?<Check size={19}/>:<Database size={19}/>}<div><strong>{data.job.message}</strong><p>{data.cachedMatches.toLocaleString()} saved matches · {data.job.requests.toLocaleString()} requests this run{data.job.finishedAt&&data.job.status==='complete'?` · ${date(data.job.finishedAt)}`:''}</p></div></div><div className="job-actions">{running?<Button variant="outline" disabled={pending||data.job.status==='pausing'} onClick={()=>handleAction('/api/pause')}><Pause/>Pause</Button>:null}</div>{!!data.job.warning&&<p className="warning">{data.job.warning}</p>}{!!data.job.anchorGaps&&<p className="warning">{data.job.anchorGaps} recent match records were unavailable. This sample may have gaps.</p>}</section>}
      <div className="section-heading"><div><p className="eyebrow">THE EVIDENCE</p><h2>Match history</h2></div><Button variant="outline" disabled={!matches.length||!online||exporting} onClick={()=>void exportData()}><ArrowDownToLine/>Export observations</Button></div>
      {!matches.length?<section className="empty"><Database size={26}/><h3>Your first comparison will appear here.</h3><p>Four teammates. Five opponents. Only the games they finished before yours.</p><div className="method-tags"><span>Same queue</span><span>Chronological histories</span><span>Missing data stays visible</span></div></section>:<section className="match-list"><Table><TableHeader><TableRow><TableHead>Match</TableHead><TableHead>Your champion</TableHead><TableHead>Teammates</TableHead><TableHead>Opponents</TableHead><TableHead>Difference</TableHead><TableHead>Coverage</TableHead><TableHead><span className="sr-only">Details</span></TableHead></TableRow></TableHeader><TableBody>{matches.map(m=><Fragment key={m.id}><TableRow data-state={active?.id===m.id?'selected':undefined}><TableCell><span className={`result ${m.win?'win':'loss'}`}>{m.win?'Win':'Loss'}</span><small className="match-date">{date(m.startedAt)}</small></TableCell><TableCell>{m.champion}<small className="match-date">{Math.floor(m.duration/60)}m {Math.floor(m.duration%60)}s</small></TableCell><TableCell>{number(m.allyMean,'%')}</TableCell><TableCell>{number(m.enemyMean,'%')}</TableCell><TableCell><span className={m.gap!==null&&m.gap<0?'negative':'positive'}>{m.gap===null?'—':`${m.gap>0?'+':''}${m.gap.toFixed(1)} pp`}</span></TableCell><TableCell><span className={`coverage ${m.complete?'ready':''}`}>{m.complete?'Complete':`${m.historiesReady}/10 processed`}</span></TableCell><TableCell><Button variant="ghost" size="sm" id={`match-toggle-${m.id}`} aria-controls={`match-details-${m.id}`} aria-expanded={active?.id===m.id} aria-label={`View ${m.champion} match from ${date(m.startedAt)}`} onClick={()=>setSelected(active?.id===m.id?null:m.id)}>Details<ChevronDown size={14} className={active?.id===m.id?'rotate-180':undefined}/></Button></TableCell></TableRow>
        {active?.id===m.id&&<TableRow className="match-details-row"><TableCell colSpan={7} className="match-details-cell">
          <section id={`match-details-${m.id}`} className="details" aria-label="Selected match details"><div className="section-heading"><div><p className="eyebrow">{active.id}</p><h2>{active.champion} · {date(active.startedAt)}</h2></div><Button variant="ghost" onClick={()=>{setSelected(null);document.getElementById(`match-toggle-${m.id}`)?.focus();}}>Close details</Button></div><div className="rank-comparison"><TeamRank label="Your team" rank={active.teamRanks?.allies}/><span className="versus">VS</span><TeamRank label="Opponents" rank={active.teamRanks?.enemies}/></div><p className="rank-explainer">Average observed rank · all five players per team, including you. Missing and unranked players are excluded. These are ranks when fetched, not historical match ranks. Each division counts as 100 LP; Master, Grandmaster and Challenger share the Master+ LP scale.</p><details className="champion-rules"><summary>How champion tags work</summary><p>{championHabitRules}</p></details><div className="team-grid">{[true,false].map(ally=><div className="team-column" key={String(ally)}><h3>{ally?'Your team':'Opponents'}</h3>{active.participants.filter(p=>(p.team===active.team)===ally).map(p=><PlayerCard key={p.puuid} player={p} duos={active.duoPairs??[]} players={active.participants}/>)}</div>)}</div><DuoPanel key={active.id} match={active} disabled={!online||pending} save={async(players,status)=>{await action('/api/duo',{matchId:active.id,players,status});}}/></section>
        </TableCell></TableRow>}
      </Fragment>)}</TableBody></Table></section>}
      {!!matches.length&&<p className="table-note">Percentages are averages of players’ prior win rates, not predictions. A difference is shown only when all nine other players have complete histories.</p>}
      <section className="methodology"><h3>How to read this pilot</h3><p>A positive difference means your four teammates had a higher average recent win rate than the five opponents. You are excluded from that comparison. This does not measure skill or the chance of winning.</p><p>Histories use Ranked Solo/Duo games completed before the shared match. Games shorter than three minutes are excluded as a remake approximation. Rank snapshots are current when fetched; account levels come from match records. Main role means the most-played role in the available prior history; ties share main-role status. Off role does not mean confirmed autofill.</p></section>
      <footer><p>Twenty matches are an exploratory sample. Overlapping histories and repeat players make observations dependent. This tool cannot establish matchmaking intent, internal MMR, or whether an account is a smurf.</p><p>Queue Context is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.</p></footer>
    </main></div>;
}

function RankStat({label}:{label:string|null}) {
  const tier=label?.split(' ')[0].replace('+','').toLowerCase();
  const hasEmblem=tier&&['iron','bronze','silver','gold','platinum','emerald','diamond','master','grandmaster','challenger'].includes(tier);
  const lp=label?.match(/ (\d+ LP)$/)?.[1];
  return <div className="rank-stat-value">
    {hasEmblem&&<img className="rank-emblem" src={`/ranks/${tier}.png`} alt="" aria-hidden="true" width={64} height={64} decoding="async"/>}
    <strong className="rank-value">{lp?label?.slice(0,-lp.length-1):label??'—'}{lp&&<small>{lp}</small>}</strong>
  </div>;
}

const championHabitLabels={unknown:'History pending',first_in_20:'First in 20',one_trick:'OTP',main:'Main',flex:'Flex'};
const championHabitRules='Based only on the 20 ranked games before this match: OTP (one-trick) = 16+ games on this champion; Main = most-played (ties allowed) with 6+ games; Flex = 1+ games otherwise; first in 20 = 0 games. Incomplete histories are not classified. These are sample-based habits, not lifetime mastery.';

function GameIcon({src,alt,className,fallback}:{src?:string;alt:string;className:string;fallback:string}) {
  const [failed,setFailed]=useState(false);
  return <div className={className}>{src&&!failed?<img src={src} alt={alt} width={56} height={56} loading="lazy" onError={()=>setFailed(true)}/>:<span aria-label={`${alt} unavailable`}>{fallback.slice(0,1)}</span>}</div>;
}

function PlayerCard({player:p,duos,players}:{player:Player;duos:DuoPair[];players:Player[]}){
  const h=p.history;const rank=p.rankSnapshot?.rank;
  const habit=h?.championHabit??'unknown';
  const iconFile=(championIcons.icons as Record<string,string>)[String(p.championId)];
  const championUrl=iconFile?`https://ddragon.leagueoflegends.com/cdn/${championIcons.version}/img/champion/${iconFile}`:undefined;
  const profileUrl=typeof p.profileIconId==='number'&&p.profileIconId>=0?`https://ddragon.leagueoflegends.com/cdn/${championIcons.version}/img/profileicon/${p.profileIconId}.png`:undefined;
  return <article className={`player league-player ${p.isSelf?'self':''}`}>
    <div className="player-identity">
      <GameIcon key={profileUrl} src={profileUrl} alt={`${p.name}'s summoner icon`} className="summoner-portrait" fallback={p.name}/>
      <div className="player-name"><strong>{p.name}</strong><span>#{p.tag}{p.isSelf?' · YOU':''}</span></div>
      <div className="player-winrate"><b>{number(h?.winRate??null,'%')}</b><span>Prior win rate</span></div>
    </div>
    <div className="champion-strip">
      <GameIcon key={championUrl} src={championUrl} alt={p.champion} className="champion-portrait" fallback={p.champion}/>
      <div className="champion-name"><strong>{p.champion}</strong><span>{roleName(p.role)} · {h?`${h.championGames}/${h.n} prior games`:'History pending'}</span></div>
      <span className={`champion-badge ${habit}`} title={championHabitRules}>{habit==='unknown'&&h?'Incomplete history':championHabitLabels[habit]}</span>
    </div>
    <div className="role-summary"><span>Main role: <strong>{h?.mainRoles?.length?h.mainRoles.join(' / '):'Unknown'}</strong>{h?.mainRoles?.length?` · ${h.mainRoleGames}/${h.n}${h.mainRoles.length>1?' each (tie)':''}`:''}</span><span className={`role-badge ${h?.roleStatus??'unknown'}`}>{h?.roleStatus==='main'?'Main role':h?.roleStatus==='off'?'Off role':'Role unknown'}{h&&!h.complete?' · partial':''}</span></div>
    {duos.filter(d=>d.status!=='not_duo'&&d.players.includes(p.puuid)).map(d=><p className={`duo-badge ${d.status}`} key={d.players.join(':')}>{d.status==='duo'?'Duo (manual)':'Possible duo'} · {players.find(other=>other.puuid===d.players.find(id=>id!==p.puuid))?.name??'Teammate'}</p>)}
    <div className="player-stats"><span>{h?`${h.wins}W / ${h.losses}L · ${h.n}/20 games`:'History pending'}</span><span>Level {p.level??'—'}</span></div>
    {h&&<><div className="history-bar" aria-label={`${h.wins} wins in ${h.n} prior games`}><span style={{width:`${h.winRate??0}%`}}/></div><div className="player-stats"><span>Role played: {h.roleGames??'—'}/{h.n}</span><span>{h.streak>0?`${h.streak}W streak`:h.streak<0?`${-h.streak}L streak`:'No streak'}</span></div>{!h.complete&&<p className="warning">Partial history{h.missing?` · ${h.missing} unavailable records`:''}; excluded from team comparison.</p>}</>}
    <p className="rank-note">{p.rankSnapshot?`${rank?`${rank.tier.charAt(0)+rank.tier.slice(1).toLowerCase()} ${['MASTER','GRANDMASTER','CHALLENGER'].includes(rank.tier)?'':rank.rank+' · '}${rank.leaguePoints} LP`:'Unranked'} · observed ${date(p.rankSnapshot.observedAt)}`:'No rank snapshot yet'}</p>
  </article>;
}

function ProfileIcon({profile}:{profile:Profile}) {
  const [failed,setFailed]=useState(false);
  return <div className="account-mark">{profile.iconUrl&&!failed?<img src={profile.iconUrl} alt={`${profile.name}'s summoner icon`} width={64} height={64} onError={()=>setFailed(true)}/>:<span>{profile.name.slice(0,1)}</span>}</div>;
}
function TeamRank({label,rank}:{label:string;rank?:RankAverage}) {
  return <div className="team-rank"><p>{label}</p><RankStat label={rank?.label??null}/><span>{rank?.count??0}/{rank?.total??5} ranked players{rank&&rank.count<rank.total?' · partial average':''}</span>{rank?.oldestObservation&&<small>Observed {date(rank.oldestObservation)}{rank.newestObservation&&date(rank.newestObservation)!==date(rank.oldestObservation)?` – ${date(rank.newestObservation)}`:''}</small>}</div>;
}

function DuoPanel({match,disabled,save}:{match:Match;disabled:boolean;save:(players:string[],status:string)=>Promise<void>}) {
  const [first,setFirst]=useState(match.participants.find(p=>p.isSelf)?.puuid??match.participants[0]?.puuid??'');
  const [second,setSecond]=useState('');
  const [message,setMessage]=useState('');
  const player=match.participants.find(p=>p.puuid===first);
  const options=match.participants.filter(p=>p.team===player?.team&&p.puuid!==first);
  const tag=async(ids:string[],status:string)=>{setMessage('');try{await save(ids,status);setMessage(status==='clear'?'Manual tag cleared.':'Duo label saved for this match.');}catch(e){setMessage(e instanceof Error?e.message:'Could not save duo label.');}};
  const pairs=match.duoPairs??[];
  return <details className="duo-panel" aria-label="Duo queue labels"><summary className="duo-heading"><Users size={16}/><span>Duo queue</span><span className="duo-count">{pairs.filter(p=>p.status==='duo').length} confirmed · {pairs.filter(p=>p.status==='possible').length} possible</span><ChevronDown size={16}/></summary><div className="duo-panel-content"><p>Possible duos shared a team in at least 3 games within both players’ available prior histories. This is a clue, not proof of a premade. No suggestion does not mean solo queued.</p>
    {pairs.length>0?<div className="duo-pairs">{pairs.map(d=><div className="duo-pair" key={d.players.join(':')}><div><span className={`duo-badge ${d.status}`}>{d.status==='duo'?'Duo — manually confirmed':d.status==='not_duo'?'Not duo — manual':'Possible duo'}</span><strong>{d.players.map(id=>match.participants.find(p=>p.puuid===id)?.name??'Player').join(' + ')}</strong><small>{d.sharedGames} earlier games on the same team{!d.historyComplete?' · incomplete histories':''}{d.recordedAt?` · tagged ${date(d.recordedAt)}`:''}</small></div><div className="duo-actions">{d.source==='history'?<><Button size="sm" variant="outline" disabled={disabled} onClick={()=>void tag(d.players,'duo')}>Confirm duo</Button><Button size="sm" variant="ghost" disabled={disabled} onClick={()=>void tag(d.players,'not_duo')}>Not duo</Button></>:<Button size="sm" variant="ghost" disabled={disabled} onClick={()=>void tag(d.players,'clear')}>Clear tag</Button>}</div></div>)}</div>:<p className="duo-empty">No duo labels yet. You can tag a pair you know queued together.</p>}
    <form className="duo-form" onSubmit={e=>{e.preventDefault();void tag([first,second],'duo');}}><div><label htmlFor="duo-first">Player</label><select id="duo-first" value={first} onChange={e=>{setFirst(e.target.value);setSecond('');}} disabled={disabled} required>{match.participants.map(p=><option key={p.puuid} value={p.puuid}>{p.name}#{p.tag} · {p.team===match.team?'Your team':'Opponents'}</option>)}</select></div><div><label htmlFor="duo-second">Duo partner</label><select id="duo-second" value={second} onChange={e=>setSecond(e.target.value)} disabled={disabled} required><option value="">Choose a teammate</option>{options.map(p=><option key={p.puuid} value={p.puuid}>{p.name}#{p.tag}</option>)}</select></div><Button type="submit" disabled={disabled||!second}>Tag known duo</Button></form><p className="duo-message" role="status">{message}</p>
  </div></details>;
}
