import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Check, ChevronRight, CirclePlay, Clapperboard,
  Film, ListVideo, LogOut, Pause, Play, Plus, Search, SkipForward, Square, Tv, Users, Volume2, X } from 'lucide-react';
import './style.css';

type Track = { index: number; title: string };
type Item = { id: string; title: string; type: string; year?: number; overview: string; duration: number;
  artwork?: string; series?: string; season?: number; episode?: number; audio: Track[]; subtitles: Track[] };
type Row = Item & { key: string; requested_by: string; requester?: User };
type Player = { current: Row | null; queue: Row[]; state: string; position: number; volume: number;
  audio: number | null; subtitle: number; channel_name: string | null; can_request: boolean; can_control: boolean; available: boolean;
  error: string; metrics: { fps?: number; av_drift_ms?: number } };
type User = { id: string; name: string; avatar?: string | null };
type Brand = { name: string; color: string; logo?: string | null; icon?: string | null;
  favicon?: string | null; favicon_dark?: string | null; backdrop?: string | null };

function applyTheme(color: string) {
  if (!/^#[a-f0-9]{6}$/i.test(color)) return;
  const rgb = color.slice(1).match(/../g)!.map(v => parseInt(v, 16));
  const mix = (base: number, weight: number) => '#' + rgb.map(v =>
    Math.round(v * weight + base * (1 - weight)).toString(16).padStart(2, '0')).join('');
  const linear = rgb.map(v => v / 255 <= .04045 ? v / 255 / 12.92 : ((v / 255 + .055) / 1.055) ** 2.4);
  const light = linear[0] * .2126 + linear[1] * .7152 + linear[2] * .0722;
  const palette: Record<string, string> = { accent: color, 'accent-text': light > .179 ? '#000000' : '#ffffff',
    text: mix(255, .03), muted: mix(255, .3), line: mix(255, .72), bg: mix(0, .08),
    panel: mix(0, .14), raised: mix(0, .2), selected: mix(0, .35), highlight: mix(255, .45) };
  Object.entries(palette).forEach(([key, value]) => document.documentElement.style.setProperty('--' + key, value));
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', color);
}

function Avatar({ person }: { person: User }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [person.avatar]);
  return <span className="avatar" aria-hidden="true">
    {person.avatar && !failed ? <img src={person.avatar} alt="" loading="lazy" referrerPolicy="no-referrer"
      onError={() => setFailed(true)} /> : Array.from(person.name)[0]?.toUpperCase() || '?'}
  </span>;
}

function Requester({ row }: { row: Row }) {
  const person = row.requester || { id: '', name: row.requested_by };
  return <span className="requester"><Avatar person={person} /><span>Requested by <strong>{person.name}</strong></span></span>;
}

const empty: Player = { current: null, queue: [], state: 'idle', position: 0, volume: 70, audio: null,
  subtitle: -1, channel_name: null, can_request: false, can_control: false, available: false, error: '', metrics: {} };
const time = (seconds: number) => {
  const n = Math.max(0, Math.floor(seconds));
  return n >= 3600 ? `${Math.floor(n / 3600)}:${String(Math.floor(n % 3600 / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`
    : `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`;
};

async function api<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', signal, ...(body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : {}) });
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.error || 'That request could not be completed.'), { status: response.status });
  return data;
}

function Poster({ item, large = false }: { item: Item; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [item.id]);
  return <div className={`poster ${large ? 'large' : ''}`}>
    {item.artwork && !failed ? <img src={item.artwork} alt="" loading="lazy" onError={() => setFailed(true)} /> :
      <div className="poster-fallback"><Film size={large ? 56 : 32} strokeWidth={1} /><span>{item.title}</span></div>}
    <span className="poster-type">{item.type === 'Series' ? 'SERIES' : item.type === 'Episode' ? `EPISODE ${item.episode ?? ''}` : item.year ?? 'FILM'}</span>
  </div>;
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [instance, setInstance] = useState('your community');
  const [brand, setBrand] = useState<Brand>({ name: 'Cinema', color: '#4641d9' });
  const [brandImageFailed, setBrandImageFailed] = useState(false);
  const [player, setPlayer] = useState<Player>(empty);
  const [libraries, setLibraries] = useState<Item[]>([]);
  const [items, setItems] = useState<Item[]>([]);
  const [crumbs, setCrumbs] = useState<Item[]>([]);
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [selected, setSelected] = useState<Item | null>(null);
  const [connected, setConnected] = useState(false);
  const [seek, setSeek] = useState<number | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const parent = crumbs.at(-1);
  const controls = player.can_request && player.can_control && connected && !busy;

  useEffect(() => {
    api<{ instance: string; branding?: Brand }>('/api/meta').then(m => {
      const name = m.branding?.name || new URL(m.instance).hostname;
      setInstance(name);
      setBrand(m.branding || { name, color: '#4641d9' });
      applyTheme(m.branding?.color || '#4641d9');
      document.title = name + ' Cinema';
      document.querySelector('meta[name="description"]')?.setAttribute('content', 'Watch together in ' + name + '.');
      [m.branding?.favicon, m.branding?.favicon_dark].forEach((url, i) => {
        if (!url) return;
        const link = document.createElement('link');
        link.rel = 'icon'; link.href = url;
        if (i) link.media = '(prefers-color-scheme: dark)';
        document.head.append(link);
      });
    }).catch(() => {});
    api<User>('/api/me').then(setUser).catch(e => { if (e.status !== 401) setError(e.message); }).finally(() => setInitializing(false));
  }, []);

  useEffect(() => {
    if (!user) return;
    api<Item[]>('/api/libraries').then(setLibraries).catch(e => setError(e.message));
    const events = new EventSource('/api/events');
    events.addEventListener('player', e => { setPlayer(JSON.parse(e.data)); setConnected(true); });
    events.addEventListener('session', e => {
      const data = JSON.parse(e.data);
      if ([401, 403].includes(data.status)) { setUser(null); events.close(); }
      setError(data.error);
    });
    events.onerror = () => setConnected(false);
    return () => events.close();
  }, [user?.id]);

  useEffect(() => {
    if (!user) return;
    const abort = new AbortController();
    setLoading(true);
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ q: query, start: String(page * 40) });
      if (parent) params.set('parent', parent.id);
      api<{ items: Item[]; total: number }>(`/api/items?${params}`, undefined, abort.signal)
        .then(data => { setItems(data.items); setTotal(data.total); })
        .catch(e => { if (e.name !== 'AbortError') setError(e.message); })
        .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    }, query ? 250 : 0);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [user?.id, query, parent?.id, page]);

  useEffect(() => {
    if (selected && dialogRef.current && !dialogRef.current.open) dialogRef.current.showModal();
    if (!selected) dialogRef.current?.close();
  }, [selected]);
  useEffect(() => { if (notice) { const t = setTimeout(() => setNotice(''), 3500); return () => clearTimeout(t); } }, [notice]);

  async function act(action: string, fields: object = {}) {
    if (!controls) return;
    setBusy(true); setError('');
    try {
      const result = await api<Player>('/api/action', { action, key: player.current?.key, ...fields });
      setPlayer(result);
      if (action === 'add') setNotice('Added to the watch queue');
      if (action === 'play') setSelected(null);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function open(item: Item) {
    if (['Series', 'Season', 'CollectionFolder'].includes(item.type)) {
      setCrumbs(c => [...c, item]); setQuery(''); setPage(0); return;
    }
    try { setSelected(await api<Item>(`/api/items/${item.id}`)); }
    catch (e) { setError((e as Error).message); }
  }

  return <div className="app">
    <header className="topbar">
      <a className="brand" href="/" aria-label={instance + ' Cinema home'}>
        {(brand.logo || brand.icon) && !brandImageFailed
          ? <img className={brand.logo ? 'brand-logo' : 'brand-symbol'} src={(brand.logo || brand.icon)!}
              alt={brand.logo ? instance : ''} onError={() => setBrandImageFailed(true)} />
          : <span className="brand-icon"><Clapperboard size={23} /></span>}
        <span>{(!brand.logo || brandImageFailed) && instance}<span className="brand-light">cinema</span></span></a>
      <div className="header-end"><span className="instance"><span className={`dot ${connected ? 'online' : ''}`} />{instance}</span>
        {user && <><Avatar person={user} /><span className="username">{user.name}</span>
          <button className="icon-button" aria-label="Sign out" onClick={() => api('/auth/logout', {}).then(() => { setUser(null); setPlayer(empty); }).catch(e => setError(e.message))}><LogOut size={17} /></button></>}
      </div>
    </header>
    <main>
      {error && <div className="alert" role="alert">{error}<button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><X size={16} /></button></div>}
      {notice && <div className="toast" role="status"><Check size={17} />{notice}</div>}
      {!user ? <section className="welcome">
        <div className="welcome-copy"><span className="eyebrow">YOUR LIBRARY. YOUR PEOPLE.</span><h1>A little cinema.<br />A shared evening.</h1>
          <p>Pick something good from Jellyfin. Settle into a {instance} voice channel. Watch it together.</p>
          <a className="button primary" href="/auth/login"><CirclePlay size={19} />{initializing ? 'Connecting…' : 'Sign in with ' + instance}<ArrowRight size={17} /></a>
          <span className="welcome-note">Your community account is your ticket.</span>
        </div>
        <div className="cinema-art" aria-hidden="true">{brand.backdrop && <img className="instance-backdrop" src={brand.backdrop} alt="" onError={e => { e.currentTarget.hidden = true; }} />}<div className="film-perfs" /><div className="screen"><span className="screen-mark"><Play size={46} fill="currentColor" /></span><span className="screen-caption">BETTER TOGETHER</span></div><div className="seats"><i /><i /><i /><i /><i /></div></div>
      </section> : <>
        <div className="page-heading"><div><span className="eyebrow">THE SHARED SCREEN</span><h1>What’s on tonight?</h1><p>A good story is better with company.</p></div>
          <span className="room-badge"><Users size={16} />{player.channel_name || 'Join a voice channel to begin'}</span></div>
        <div className="layout">
          <section className="catalog" aria-label="Jellyfin library">
            <div className="catalog-toolbar"><div className="tabs"><button className={!parent ? 'active' : ''} onClick={() => { setCrumbs([]); setPage(0); setQuery(''); }}><Film size={15} />All titles</button>
              {libraries.map(l => <button key={l.id} className={crumbs[0]?.id === l.id ? 'active' : ''} onClick={() => { setCrumbs([l]); setPage(0); setQuery(''); }}>{l.title}</button>)}</div>
              <label className="search"><Search size={17} /><input aria-label="Search library" placeholder="Find a film or show" value={query} onChange={e => { setQuery(e.target.value); setPage(0); }} />{query && <button className="icon-button" aria-label="Clear search" onClick={() => setQuery('')}><X size={14} /></button>}</label>
            </div>
            {parent && <nav className="breadcrumbs" aria-label="Library navigation"><button onClick={() => { setCrumbs([]); setPage(0); }}>Library</button>{crumbs.map((c, i) => <React.Fragment key={c.id}><ChevronRight size={13} /><button onClick={() => { setCrumbs(crumbs.slice(0, i + 1)); setPage(0); }}>{c.title}</button></React.Fragment>)}</nav>}
            <div className="section-line"><h2>{query ? `Results for “${query}”` : parent?.title || 'Explore your library'}</h2><span>{total} titles</span></div>
            {loading ? <div className="poster-grid" aria-label="Loading titles">{Array.from({ length: 8 }, (_, i) => <div className="skeleton" key={i} />)}</div> : items.length ?
              <div className="poster-grid">{items.map(item => <button className="title-card" key={item.id} onClick={() => open(item)}><Poster item={item} /><strong>{item.title}</strong><span>{item.type === 'Series' ? 'TV series' : item.type === 'Season' ? 'Season' : item.type === 'Episode' ? `Episode ${item.episode ?? ''}` : item.year ?? 'Movie'}{item.duration > 0 ? ` · ${Math.round(item.duration / 60)} min` : ''}</span></button>)}</div> :
              <div className="empty-catalog"><Search size={30} /><h3>{query ? 'No matching titles' : 'Nothing here yet'}</h3><p>{query ? 'Try another title or browse a different library.' : 'Titles shared by your Jellyfin account will appear here.'}</p></div>}
            {total > 40 && <div className="pagination"><button className="button secondary" disabled={!page} onClick={() => setPage(page - 1)}><ArrowLeft size={16} />Previous</button><span>Page {page + 1} of {Math.ceil(total / 40)}</span><button className="button secondary" disabled={(page + 1) * 40 >= total} onClick={() => setPage(page + 1)}>Next<ArrowRight size={16} /></button></div>}
          </section>
          <aside className="watch-panel">
            <div className="panel-heading"><span className="eyebrow">NOW SHOWING</span><span className={`status ${player.state === 'playing' ? 'live' : ''}`}>{player.state === 'idle' ? 'READY WHEN YOU ARE' : player.state.toUpperCase()}</span></div>
            {player.current ? <><button className="now-art" onClick={() => open(player.current!)}><Poster item={player.current} large /><span className="now-overlay"><Play size={25} /></span></button><h2 className="now-title">{player.current.title}</h2><div className="credit"><Requester row={player.current} /></div>
              <div className="seek"><input type="range" aria-label="Playback position" min="0" max={Math.max(1, player.current.duration)} step="1" value={seek ?? Math.min(player.position, player.current.duration)} disabled={!controls}
                onChange={e => setSeek(Number(e.target.value))} onPointerUp={e => { act('seek', { position: Number(e.currentTarget.value) }); setSeek(null); }}
                onKeyUp={e => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) { act('seek', { position: Number(e.currentTarget.value) }); setSeek(null); } }} />
                <div><span>{time(seek ?? player.position)}</span><span>{time(player.current.duration)}</span></div></div>
              <div className="playback-buttons"><button className="icon-button" aria-label="Stop and disconnect" disabled={!controls} onClick={() => act('stop')}><Square size={18} /></button>
                <button className="play-button" aria-label={player.state === 'paused' || player.state === 'error' ? 'Resume' : 'Pause'} disabled={!controls} onClick={() => act(player.state === 'paused' || player.state === 'error' ? 'resume' : 'pause')}>{player.state === 'paused' || player.state === 'error' ? <Play size={23} fill="currentColor" /> : <Pause size={23} fill="currentColor" />}</button>
                <button className="icon-button" aria-label="Skip title" disabled={!controls} onClick={() => act('skip')}><SkipForward size={22} /></button></div>
              <label className="volume"><Volume2 size={17} /><input aria-label="Shared volume" type="range" min="0" max="100" value={player.volume} disabled={!controls} onChange={e => setPlayer({ ...player, volume: Number(e.target.value) })} onPointerUp={e => act('volume', { value: Number(e.currentTarget.value) })} onKeyUp={e => act('volume', { value: Number(e.currentTarget.value) })} /><span>{player.volume}%</span></label>
              <div className="track-selects"><label>Audio<select aria-label="Audio track" value={player.audio ?? ''} disabled={!controls} onChange={e => act('tracks', { audio: e.target.value ? Number(e.target.value) : null, subtitle: player.subtitle })}><option value="">Default</option>{player.current.audio.map(t => <option key={t.index} value={t.index}>{t.title}</option>)}</select></label>
                <label>Subtitles<select aria-label="Subtitle track" value={player.subtitle} disabled={!controls} onChange={e => act('tracks', { audio: player.audio, subtitle: Number(e.target.value) })}><option value={-1}>Off</option>{player.current.subtitles.map(t => <option key={t.index} value={t.index}>{t.title}</option>)}</select></label></div>
              <p className="shared-note">Playback and track choices are shared by everyone.</p>
            </> : <div className="empty-player"><div className="empty-screen"><Tv size={43} strokeWidth={1} /><span className="empty-screen-dot" /></div><h3>The screen is yours.</h3><p>Choose a movie or episode.<br />We’ll take care of the popcorn mood.</p></div>}
            {!connected && <p className="connection-note">Connecting to the player…</p>}
            {connected && !player.can_request && <p className="connection-note">You have read-only access. Ask an administrator for permission to request titles and control playback.</p>}
            {connected && player.can_request && !player.can_control && <p className="connection-note">Join {player.channel_name || 'a permitted ' + instance + ' voice channel'} to use the controls.</p>}
            {player.error && <p className="player-error" role="status">{player.error}</p>}
            <div className="queue-heading"><h3><ListVideo size={18} />Up next</h3><span>{player.queue.length}</span></div>
            {player.queue.length ? <ol className="queue">{player.queue.map((row, i) => <li key={row.key}><span className="queue-number">{String(i + 1).padStart(2, '0')}</span><div><strong>{row.title}</strong><Requester row={row} /></div><div className="queue-actions"><button className="icon-button" aria-label={`Move ${row.title} up`} disabled={!controls || i === 0} onClick={() => act('move', { key: row.key, index: i - 1 })}><ArrowUp size={14} /></button><button className="icon-button" aria-label={`Move ${row.title} down`} disabled={!controls || i === player.queue.length - 1} onClick={() => act('move', { key: row.key, index: i + 1 })}><ArrowDown size={14} /></button><button className="icon-button" aria-label={`Remove ${row.title}`} disabled={!controls} onClick={() => act('remove', { key: row.key })}><X size={14} /></button></div></li>)}</ol> : <p className="empty-queue">A blank slate for the evening.<br />Add a title to keep the stories coming.</p>}
            <div className="panel-footer"><span className="dot online" />Video and audio play in {instance}</div>
          </aside>
        </div>
      </>}
      <footer><span>Jellyfin library. {instance} company.</span><span>Made for a shared screen <span className="footer-star">✳</span></span></footer>
    </main>
    <dialog ref={dialogRef} onCancel={() => setSelected(null)} onClick={e => { if (e.target === e.currentTarget) setSelected(null); }}>
      {selected && <div className="detail"><button className="icon-button detail-close" aria-label="Close title details" onClick={() => setSelected(null)}><X size={21} /></button><Poster item={selected} large /><div className="detail-copy"><span className="eyebrow">{selected.series || (selected.type === 'Movie' ? 'FEATURE FILM' : 'EPISODE')}</span><h2>{selected.title}</h2><p className="detail-meta">{selected.year}{selected.duration ? ` · ${Math.round(selected.duration / 60)} min` : ''}</p><p className="overview">{selected.overview || 'No description available for this title.'}</p><div className="detail-buttons"><button className="button primary" disabled={!controls} onClick={() => act('play', { item_id: selected.id })}><Play size={17} fill="currentColor" />{player.current ? 'Play now' : 'Start watching'}</button><button className="button secondary" disabled={!controls} onClick={() => act('add', { item_id: selected.id })}><Plus size={18} />Add to queue</button></div>{!controls && <p className="shared-note">{connected && !player.can_request ? 'You have read-only access. Ask an administrator for permission to request titles.' : 'Join the active voice channel to play this title.'}</p>}</div></div>}
    </dialog>
  </div>;
}

createRoot(document.getElementById('root')!).render(<App />);
