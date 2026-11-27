import { useSyncExternalStore } from 'react';
import { ApiError, api, isAbort } from '../api/client';
import type { PlayEventIn, WaveFilters, WaveGenre, WaveOrigin, WaveOut, WaveSeedIn, WaveTrackOut } from '../api/types';
import { EMBEDDED } from './embedded';
import { applyUpcomingDelta, continueCurrentInList, endWavePrefetch, getListenState, getQueueRevision, listenAll, replaceUpcoming, subscribeListen, type ListenTrack } from './listen';
import { canPlayAll, canPlayWave, playWave } from './player';
import { onPlayEvent } from './plays';
import { toast, toastError } from './ui';

/** «Моя волна» (MIXES.md 7): an endless stream the Hub picks for the user, 5 tracks at a time
 * (`POST /api/wave/start`, then `POST /api/wave/{id}/next`).
 *
 * In a browser (and app versions whose player doesn't play catalog tracks) the page's own mini player plays it
 * (`lib/listen.ts`, context `wave:<id>`): each time a wave track starts, and after a ♥ of one, the events since the
 * last call go to the Hub with the refs still ahead, and the answer updates the tail with a delta — so the
 * stream is picked again after every action. The same events go to /api/plays/events as always (the Hub stores an
 * event_id once).
 *
 * Inside the app (1.2.0+) one player plays, the native one: the wave is handed to it whole through the bridge
 * `playWave(waveJson)` (app 1.6.0+, `lib/player.ts`): the answer of /start; the app plays it and asks `next`
 * itself (`HubWave.kt`), with the same events. An app with `playQueue` but without `playWave` gets no wave
 * buttons (5 tracks without a continuation would be worse). Another list started in the page player ends the wave. */

/** The wave can play here: the page's own player, or the app's player that knows waves. */
export function canWave(): boolean {
  return !canPlayAll() || canPlayWave();
}

const RETRY_MS = 4000;
const FILTER_RESTART_MS = 600;
export const WAVE_GENRES: readonly WaveGenre[] = ['rock', 'metal', 'electronic', 'hiphop', 'pop', 'indie', 'jazz', 'classical'];
export const EMPTY_WAVE_FILTERS: WaveFilters = { genres: [], lang: null, familiarity: null, energy: null };

interface Session {
  id: string;
  own: boolean;
  requestedFilters: WaveFilters;
  title: string;
  context: string;
  refs: Set<string>;
  songs: Set<string>;
  /** events of the wave's tracks not yet handed to the Hub */
  pending: PlayEventIn[];
  inFlight: PlayEventIn[];
  ctl?: AbortController;
  retry?: ReturnType<typeof setTimeout>;
  again: boolean;
}

let session: Session | null = null;
let sessionAccount: number | null = null;
const SESSION_KEY = 'hm-wave-session:';
const FILTERS_KEY = 'hm-wave-filters:';
let starting: string | null = null; // the seed being started ('' — the user's own wave)
let startRequest: AbortController | null = null;
let filterRestart: ReturnType<typeof setTimeout> | undefined;
let ownFilters = EMPTY_WAVE_FILTERS;
let filterRelaxed: string[] = [];
const subscribers = new Set<() => void>();

function normalizeFilters(value: unknown): WaveFilters {
  if (!value || typeof value !== 'object') return EMPTY_WAVE_FILTERS;
  const { genres, lang, familiarity, energy } = value as Partial<Record<keyof WaveFilters, unknown>>;
  return {
    genres: Array.isArray(genres) ? WAVE_GENRES.filter((genre) => genres.includes(genre)) : [],
    lang: lang === 'ru' || lang === 'foreign' ? lang : null,
    familiarity: familiarity === 'known' || familiarity === 'new' ? familiarity : null,
    energy: energy === 'high' || energy === 'low' ? energy : null,
  };
}

function sameFilters(a: WaveFilters, b: WaveFilters): boolean {
  return a.lang === b.lang && a.familiarity === b.familiarity && a.energy === b.energy
    && a.genres.length === b.genres.length && a.genres.every((genre) => b.genres.includes(genre));
}

function waveSnapshot() {
  return {
    title: session?.title ?? null, starting, filters: ownFilters, filterRelaxed,
    filtersChanged: !!session?.own && !sameFilters(session.requestedFilters, ownFilters),
  };
}
let snapshot = waveSnapshot();

function emit() {
  persistWave();
  snapshot = waveSnapshot();
  for (const fn of subscribers) fn();
}

function cancelFilterRestart() {
  clearTimeout(filterRestart);
  filterRestart = undefined;
}

function cancelStart() {
  startRequest?.abort();
  startRequest = null;
  starting = null;
}

function ownWaveRunning(): boolean {
  const { context, phase } = getListenState();
  return !!session?.own && context === session.context
    && (phase === 'playing' || phase === 'loading' || phase === 'resolving');
}

export function setWaveFilters(filters: WaveFilters, origin: WaveOrigin) {
  const next = normalizeFilters(filters);
  if (sameFilters(next, ownFilters)) return;
  ownFilters = next;
  filterRelaxed = [];
  if (sessionAccount !== null) {
    try { localStorage.setItem(FILTERS_KEY + sessionAccount, JSON.stringify(next)); } catch { /* Keep the selection in memory. */ }
  }
  const restarting = starting === '' || filterRestart !== undefined;
  cancelFilterRestart();
  if (!canPlayWave() && (restarting || (starting === null && ownWaveRunning()))) {
    if (restarting) cancelStart();
    filterRestart = setTimeout(() => {
      filterRestart = undefined;
      if (starting !== null || (!restarting && (!ownWaveRunning() || (session && sameFilters(session.requestedFilters, next))))) return;
      void startWave(null, origin, next);
    }, FILTER_RESTART_MS);
  }
  emit();
}

function persistWave() {
  if (sessionAccount === null) return;
  try {
    const key = SESSION_KEY + sessionAccount;
    if (!session) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify({ id: session.id, title: session.title,
      own: session.own, requestedFilters: session.requestedFilters,
      refs: [...session.refs], songs: [...session.songs], pending: [...session.inFlight, ...session.pending] }));
  } catch { /* The current wave can still play without storage. */ }
}

/** Continue the same server session, including events that were still awaiting acknowledgement. */
export function restoreWave(account: number | null) {
  if (sessionAccount === account) return;
  cancelStart();
  end();
  sessionAccount = account;
  ownFilters = EMPTY_WAVE_FILTERS;
  if (account !== null) {
    try { ownFilters = normalizeFilters(JSON.parse(localStorage.getItem(FILTERS_KEY + account) ?? 'null')); } catch { /* Use defaults. */ }
  }
  const st = getListenState();
  if (account !== null && !canPlayAll() && st.track && st.context.startsWith('wave:')) {
    const id = st.context.slice('wave:'.length);
    let saved: { id?: string; title?: string; own?: boolean; requestedFilters?: unknown;
      refs?: string[]; songs?: string[]; pending?: PlayEventIn[] } | null = null;
    try { saved = JSON.parse(localStorage.getItem(SESSION_KEY + account) ?? 'null'); } catch { /* rebuild from the queue */ }
    if (saved?.id !== id) saved = null;
    const title = typeof saved?.title === 'string' ? saved.title : 'Моя волна';
    session = { id, title, own: saved?.own ?? (title === 'Моя волна'), context: st.context,
      requestedFilters: normalizeFilters(saved?.requestedFilters),
      refs: new Set([...(Array.isArray(saved?.refs) ? saved.refs.filter((r) => typeof r === 'string') : []), ...st.queue.map((t) => t.ref)]),
      songs: new Set(Array.isArray(saved?.songs) ? saved.songs.filter((r) => typeof r === 'string') : []),
      pending: Array.isArray(saved?.pending) ? saved.pending.filter((e) => e && typeof e.event_id === 'string') : [],
      inFlight: [], again: false };
  }
  emit();
}

function subscribe(fn: () => void) {
  subscribers.add(fn);
  return () => {
    subscribers.delete(fn);
  };
}

/** The playing wave's title (null — none) and the seed being started. */
export function useWave() {
  return useSyncExternalStore(subscribe, () => snapshot);
}

export const seedKey = (seed?: WaveSeedIn | null) => (seed?.by_artist ? 'artist:' : '') + (seed?.ref ?? seed?.nd_song_id ?? seed?.artist_ref ?? seed?.nd_artist_id ?? '');

type SeedTrack = { ref?: string | null; nd_song_id?: string | null; artist_ref?: string | null };
export function trackSeed(t: SeedTrack): WaveSeedIn | null {
  const ref = t.ref?.trim();
  if (ref && /^(dz|nd):.+/.test(ref)) return { ref };
  const id = t.nd_song_id?.trim();
  if (id) return { nd_song_id: id };
  return ref && /^file:[^:]+:.+/.test(ref) ? { ref } : null;
}
export function artistSeed(t: SeedTrack): WaveSeedIn | null {
  const ref = t.artist_ref?.trim();
  if (ref && /^dz:.+/.test(ref)) return { artist_ref: ref };
  const track = trackSeed(t);
  return track && !track.ref?.startsWith('file:') ? { ...track, by_artist: true } : null;
}

const listenTrack = (t: WaveTrackOut): ListenTrack => ({
  ref: t.ref,
  title: t.title,
  artist: t.artists?.length ? t.artists.join(', ') : t.artist,
  cover_url: t.cover_url,
  cover_small_url: t.cover_small_url,
  album_ref: t.album_ref,
  artist_ref: t.artist_ref,
  artists: (t.artists?.length ? t.artists : [t.artist]).map((name) =>
    ({ name, ...(t.artists?.length && name === t.artist && t.artist_ref ? { ref: t.artist_ref } : {}) })),
  album: t.album_title ?? undefined, isrc: t.isrc ?? undefined,
  year: t.release_date ? Number(t.release_date.slice(0, 4)) || undefined : undefined,
  duration_s: t.duration_s,
});

function hasDelta(w: WaveOut): boolean {
  return Array.isArray(w.keep) && Array.isArray(w.add);
}

function remember(s: Session, tracks: readonly WaveTrackOut[]) {
  for (const t of tracks) {
    s.refs.add(t.ref);
    if (t.nd_song_id) s.songs.add(t.nd_song_id);
  }
}

function waitForRetry(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, RETRY_MS);
    if (signal.aborted) abort();
    else signal.addEventListener('abort', abort, { once: true });
  });
}

function logWaveError(operation: 'start' | 'next', error: unknown) {
  console.error(`Моя волна: ${operation}`, {
    status: error instanceof ApiError ? error.status : undefined,
    body: error instanceof ApiError ? error.responseBody : undefined,
    stack: error instanceof Error ? error.stack : undefined,
    error,
  });
}

/** Starts the user's own wave, or one from a track or an artist («Волна по треку»). `origin` — where it was started
 * (INSIGHTS.md 6.3: analytics only, the Hub keeps it with the session). */
export async function startWave(seed: WaveSeedIn | null | undefined, origin: WaveOrigin, filters?: WaveFilters): Promise<boolean> {
  if (startRequest) return false;
  cancelFilterRestart();
  const own = !seed;
  const selected = filters ?? (own && !canPlayWave() ? ownFilters : undefined);
  const ctl = new AbortController();
  startRequest = ctl;
  starting = seedKey(seed);
  emit();
  try {
    let w = await api.waveStart({ seed: seed ?? null, origin, client: EMBEDDED ? 'pwa-app' : 'pwa' }, ctl.signal, selected);
    const native = canPlayWave();
    const requestedFilters = selected ?? EMPTY_WAVE_FILTERS;
    // No queue has started yet: poll the same session without queued refs.
    const picksOf = (batch: WaveOut) => !native && hasDelta(batch) ? batch.add : batch.tracks;
    while (!ctl.signal.aborted && !picksOf(w).length && w.pending && !w.filter_relaxed?.includes('exhausted')) {
      await waitForRetry(ctl.signal);
      w = await api.waveNext(w.session_id, { played: [], queued: [] }, ctl.signal);
    }
    if (ctl.signal.aborted) return false;
    const picks = picksOf(w);
    const relaxed = w.filter_relaxed ?? [];
    if (!picks.length) {
      const panelOwnsMessage = own && sameFilters(requestedFilters, ownFilters);
      if (panelOwnsMessage) filterRelaxed = relaxed;
      if (relaxed.includes('exhausted')) {
        if (!panelOwnsMessage) toast('По этим фильтрам треки закончились');
      } else toast('Волна пока пустая');
      return false;
    }
    if (native) {
      end(); // a wave the page's own player had is over: the app's player plays now
      playWave(w);
      return true;
    }
    const s: Session = { id: w.session_id, title: w.title, own, requestedFilters, context: `wave:${w.session_id}`,
      refs: new Set(), songs: new Set(), pending: [], inFlight: [], again: false };
    remember(s, picks);
    const tracks = picks.map(listenTrack);
    // Keep the current playback position when the playing song is the wave seed.
    const continued = continueCurrentInList(tracks, s.context, () => {
      end(true);
      session = s;
    });
    if (!continued) {
      end(true);
      listenAll(tracks, { shuffle: false, context: s.context });
      session = s;
    }
    if (own && sameFilters(requestedFilters, ownFilters)) filterRelaxed = relaxed;
    return true;
  } catch (e) {
    if (!ctl.signal.aborted && !isAbort(e)) {
      logWaveError('start', e);
      toastError(e);
    }
    return false;
  } finally {
    if (startRequest === ctl) {
      startRequest = null;
      starting = null;
      emit();
    }
  }
}

/** Send the refs still ahead with new events, then apply the delta to that same queue revision. */
async function ask() {
  const s = session;
  if (!s) return;
  if (s.ctl) {
    s.again = true;
    return;
  }
  clearTimeout(s.retry);
  s.retry = undefined;
  const ctl = new AbortController();
  s.ctl = ctl;
  const played = s.pending.splice(0);
  s.inFlight = played;
  persistWave();
  const st = getListenState();
  const queueRevision = getQueueRevision();
  const queued = st.queue.slice(st.index + 1).map((t) => t.ref);
  try {
    const w = await api.waveNext(s.id, { played, queued }, ctl.signal);
    if (session !== s || ctl.signal.aborted || getListenState().context !== s.context) return;
    if (getQueueRevision() !== queueRevision) {
      // The server acknowledged the events, but the delta was for a different queue snapshot.
      s.again = true;
      return;
    }
    if (hasDelta(w)) {
      remember(s, w.add);
      applyUpcomingDelta(w.keep, w.add.map(listenTrack));
    } else {
      remember(s, w.tracks);
      if (w.tracks.length || !w.pending) replaceUpcoming(w.tracks.map(listenTrack));
    }
    if (session !== s) return;
    const relaxed = w.filter_relaxed ?? [];
    const exhausted = relaxed.includes('exhausted');
    const panelOwnsMessage = s.own && sameFilters(s.requestedFilters, ownFilters);
    if (panelOwnsMessage) {
      filterRelaxed = relaxed;
      emit();
    }
    const current = getListenState();
    if (!exhausted && w.pending && current.queue.length - current.index - 1 < (w.target_len ?? 5)) {
      s.retry = setTimeout(() => { if (session === s) void ask(); }, RETRY_MS);
    } else if (!w.tracks.length) {
      if (exhausted) {
        if (!panelOwnsMessage) toast('По этим фильтрам треки закончились');
      } else if (!w.pending) toast('Волна пока пустая');
    }
  } catch (e) {
    if (session !== s || ctl.signal.aborted) return;
    if (e instanceof ApiError && e.status === 404) {
      end(); // the session is over (a day without it): the queue plays out
    } else {
      if (!isAbort(e)) logWaveError('next', e);
      s.pending.unshift(...played); // retry with the next call
    }
  } finally {
    if (s.ctl === ctl) {
      s.ctl = undefined;
      s.inFlight = [];
      persistWave();
      if (s.again && session === s) {
        s.again = false;
        void ask();
      }
    }
  }
}

function cancelWork(s: Session) {
  clearTimeout(s.retry);
  s.retry = undefined;
  s.ctl?.abort();
  s.ctl = undefined;
  s.pending.unshift(...s.inFlight);
  s.inFlight = [];
  s.again = false;
}

function end(keepFilterRestart = false) {
  if (!keepFilterRestart) cancelFilterRestart();
  const s = session;
  session = null;
  filterRelaxed = [];
  if (s) {
    cancelWork(s);
    endWavePrefetch(s.context);
  }
  emit();
}

/** Own the wave's subscriptions and retry work at the app level, across player views. */
export function mountWave(): () => void {
  window.addEventListener('pagehide', persistWave);
  const unsubscribeEvents = onPlayEvent(handlePlayEvent);
  let previousPhase = getListenState().phase;
  const unsubscribeListen = subscribeListen(() => {
    const st = getListenState();
    const justEnded = previousPhase !== 'ended' && st.phase === 'ended';
    previousPhase = st.phase;
    if (session && (!st.track || st.context !== session.context)) end();
    else if (session && justEnded) void ask();
  });
  return () => {
    window.removeEventListener('pagehide', persistWave);
    unsubscribeEvents();
    unsubscribeListen();
    cancelFilterRestart();
    cancelStart();
    if (session) cancelWork(session);
    emit();
  };
}

function handlePlayEvent(e: PlayEventIn) {
  if (e.kind === 'progress' || e.kind === 'interrupted' || e.kind === 'error') return;
  if ((e.target_type && e.target_type !== 'track') || e.reverts_event_id) return;
  const s = session;
  if (!s) return;
  if (getListenState().context !== s.context) return end(); // another list plays now
  if (!(e.ref && s.refs.has(e.ref)) && !(e.nd_song_id && s.songs.has(e.nd_song_id))) return;
  s.pending.push(e);
  persistWave();
  if (e.kind === 'start' || e.kind === 'like' || e.kind === 'dislike') void ask();
}
