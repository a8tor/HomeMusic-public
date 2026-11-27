import { useEffect, useId, useState, type ReactNode } from 'react';
import type { WaveFilters as Filters, WaveGenre, WaveOrigin } from '../api/types';
import { waveFiltersSupported } from '../lib/capabilities';
import { EMPTY_WAVE_FILTERS, WAVE_GENRES, setWaveFilters, useWave } from '../lib/wave';
import { Button, cx } from './ui';

const GENRE_LABELS: Record<WaveGenre, string> = {
  rock: 'Рок', metal: 'Метал', electronic: 'Электроника', hiphop: 'Хип-хоп',
  pop: 'Поп', indie: 'Инди', jazz: 'Джаз', classical: 'Классика',
};
const LANG_LABELS = { ru: 'Русская', foreign: 'Иностранная' } as const;
const FAMILIARITY_LABELS = { known: 'Знакомое', new: 'Незнакомое' } as const;
const ENERGY_LABELS = { high: 'Энергичная', low: 'Спокойная' } as const;

const RELAXED_LABELS: Readonly<Record<string, string | undefined>> = {
  genres: 'по жанру', familiarity: 'по знакомости', energy: 'по энергии',
};

export function useWaveFilterPanel(enabled: boolean) {
  const { filters, filterRelaxed } = useWave();
  const [available, setAvailable] = useState(false);
  const [open, setOpen] = useState(false);
  const panelId = useId();
  useEffect(() => {
    if (!enabled) return;
    let mounted = true;
    void waveFiltersSupported().then((supported) => { if (mounted) setAvailable(supported); });
    return () => { mounted = false; };
  }, [enabled]);
  const labels = [
    ...filters.genres.map((genre) => GENRE_LABELS[genre]),
    ...(filters.familiarity ? [FAMILIARITY_LABELS[filters.familiarity]] : []),
    ...(filters.energy ? [ENERGY_LABELS[filters.energy]] : []),
    ...(filters.lang ? [LANG_LABELS[filters.lang]] : []),
  ];
  return { available: enabled && available, open, setOpen, panelId, filters, filterRelaxed, labels };
}

type Panel = ReturnType<typeof useWaveFilterPanel>;

export function WaveFilterTrigger({ panel, compact = false }: { panel: Panel; compact?: boolean }) {
  if (!panel.available) return null;
  const count = panel.labels.length;
  return (
    <button
      type="button"
      aria-label={count ? `Фильтры волны: выбрано ${count}` : 'Фильтры волны'}
      aria-expanded={panel.open}
      aria-controls={panel.panelId}
      onClick={() => panel.setOpen(!panel.open)}
      className={cx(
        'relative inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center gap-2 rounded-full text-sm transition hover:bg-hover',
        compact ? 'text-fg' : 'border border-line bg-surface px-4 text-fg',
      )}
    >
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
        <path d="M3 6h5m4 0h9M3 12h10m4 0h4M3 18h3m4 0h11M8 4v4m9 2v4M10 16v4" />
      </svg>
      {!compact && 'Фильтры'}
      {count > 0 && (
        <span aria-hidden="true" className={cx('rounded-full bg-accent px-1.5 text-xs font-semibold text-on-accent', compact && 'absolute right-0 top-0')}>
          {count}
        </span>
      )}
    </button>
  );
}

export function WaveFilterSummary({ panel }: { panel: Panel }) {
  if (!panel.available || !panel.labels.length) return null;
  return <span className="mt-1 block break-words text-xs leading-snug text-muted">{panel.labels.join(' · ')}</span>;
}

function Chip({ active, disabled, onClick, children }: {
  active: boolean; disabled?: boolean; onClick?: () => void; children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      className={cx(
        'min-h-11 rounded-full border px-3 text-sm transition disabled:opacity-50',
        active ? 'border-accent bg-accent/15 text-accent' : 'border-line bg-raised text-fg enabled:hover:bg-hover',
      )}
    >
      {children}
    </button>
  );
}

export function WaveFilters({ panel, origin }: { panel: Panel; origin: WaveOrigin }) {
  if (!panel.available) return null;
  const { filters, filterRelaxed } = panel;
  const change = (next: Filters) => setWaveFilters(next, origin);
  const reset = () => change(EMPTY_WAVE_FILTERS);
  const exhausted = filterRelaxed.length === 1 && filterRelaxed[0] === 'exhausted';
  const axes = filterRelaxed.map((axis) => RELAXED_LABELS[axis]).filter(Boolean).join(', ');

  return (
    <>
      <section
        id={panel.panelId}
        aria-label="Фильтры Моей волны"
        hidden={!panel.open}
        className="mt-2 space-y-3 rounded-xl border border-line bg-surface p-3 text-left text-fg"
      >
        <fieldset className="min-w-0">
          <legend className="mb-1 text-sm font-semibold">Жанры {filters.genres.length === 0 && <span className="font-normal text-muted">· любой жанр</span>}</legend>
          <div className="flex flex-wrap gap-1.5">
            {WAVE_GENRES.map((genre) => {
              const active = filters.genres.includes(genre);
              return (
                <Chip key={genre} active={active} onClick={() => change({
                  ...filters, genres: active ? filters.genres.filter((value) => value !== genre) : [...filters.genres, genre],
                })}>
                  {GENRE_LABELS[genre]}
                </Chip>
              );
            })}
          </div>
        </fieldset>
        <fieldset className="min-w-0">
          <legend className="mb-1 text-sm font-semibold">Язык</legend>
          <div className="flex flex-wrap gap-1.5">
            {(['ru', 'foreign'] as const).map((lang) => (
              <Chip key={lang} active={filters.lang === lang} onClick={() => change({ ...filters, lang: filters.lang === lang ? null : lang })}>
                {LANG_LABELS[lang]}
              </Chip>
            ))}
          </div>
        </fieldset>
        <fieldset className="min-w-0">
          <legend className="mb-1 text-sm font-semibold">Знакомость</legend>
          <div className="flex flex-wrap gap-1.5">
            {(['known', 'new'] as const).map((familiarity) => (
              <Chip key={familiarity} active={filters.familiarity === familiarity} onClick={() => change({
                ...filters, familiarity: filters.familiarity === familiarity ? null : familiarity,
              })}>
                {FAMILIARITY_LABELS[familiarity]}
              </Chip>
            ))}
          </div>
        </fieldset>
        <fieldset className="min-w-0" disabled>
          <legend className="mb-1 text-sm font-semibold">Энергия <span className="font-normal text-muted">· скоро</span></legend>
          <div className="flex flex-wrap gap-1.5">
            <Chip active={false} disabled>{ENERGY_LABELS.high}</Chip>
            <Chip active={false} disabled>{ENERGY_LABELS.low}</Chip>
          </div>
        </fieldset>
        {panel.labels.length > 0 && <Button variant="ghost" className="text-muted" onClick={reset}>Сбросить</Button>}
      </section>
      <div role="status">
        {exhausted ? (
          <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 rounded-xl border border-line bg-surface px-3 py-1.5 text-sm text-fg">
            <p>По этим фильтрам треки закончились</p>
            <Button variant="ghost" onClick={reset}>Сбросить фильтры</Button>
          </div>
        ) : filterRelaxed.length > 0 && (
          <p className="mt-2 text-xs text-muted">Фильтр узковат — {axes ? `ослабили ${axes}` : 'ослабили настройки'}</p>
        )}
      </div>
    </>
  );
}
