import { useListen } from '../lib/listen';
import { useRoute } from '../lib/router';
import { canPlayWave } from '../lib/player';
import { canWave, startWave, useWave } from '../lib/wave';
import { Icon } from './Icon';
import { Spinner } from './ui';
import { WaveFilterSummary, WaveFilterTrigger, WaveFilters, useWaveFilterPanel } from './WaveFilters';

/** «Моя волна» in the catalog, next to «Микс дня»: a tap starts the user's own endless stream. */
export function WaveCard() {
  const { title, starting } = useWave();
  const playing = useListen((s) => !!s.track && s.context.startsWith('wave:'));
  const { path } = useRoute();
  const available = canWave();
  const filterPanel = useWaveFilterPanel(available && !canPlayWave());
  if (!available) return null;
  const busy = starting === '';
  const on = playing && title === 'Моя волна';
  const origin = path.startsWith('/home') ? 'home_card' : 'catalog_card';

  return (
    <div className="mx-4 mt-3 w-[calc(100%-2rem)]">
      <div className="relative">
        <button
          type="button"
          onClick={() => void startWave(null, origin)}
          disabled={starting !== null}
          aria-label="Моя волна: слушать"
          className="flex w-full items-center gap-3 rounded-xl bg-raised p-3 text-left transition hover:bg-hover disabled:opacity-80"
        >
          <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent to-accent/50 text-on-accent">
            {busy ? <Spinner size={24} /> : <Icon name="wave" size={30} />}
          </span>
          <span className="min-w-0 flex-1 text-left">
            <span className="block font-bold">Моя волна</span>
            <span className="block text-[13px] leading-snug text-muted">
              {busy ? 'Подбираем первые треки…' : on ? 'Играет — подстраивается под каждый пропуск и ♥' : 'Бесконечный поток под ваш вкус'}
            </span>
            <WaveFilterSummary panel={filterPanel} />
          </span>
          {filterPanel.available && <span aria-hidden="true" className="w-11 shrink-0" />}
          {(!busy || filterPanel.available) && (
            <Icon name={on ? 'refresh' : 'play'} size={22} className={`shrink-0 text-accent ${busy ? 'invisible' : ''}`} />
          )}
        </button>
        {filterPanel.available && (
          <div className="absolute right-[46px] top-1/2 -translate-y-1/2">
            <WaveFilterTrigger panel={filterPanel} compact />
          </div>
        )}
      </div>
      <WaveFilters panel={filterPanel} origin={origin} />
    </div>
  );
}
