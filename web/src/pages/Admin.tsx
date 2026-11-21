import { useState, type FormEvent, type ReactNode } from 'react';
import { api } from '../api/client';
import type { AdminConfigOut, AdminStorageOut, AdminUserOut, InviteOut } from '../api/types';
import { Icon } from '../components/Icon';
import { Button, Empty, ErrorState, Field, IconButton, Notice, PageHeader, ProgressBar, SectionTitle, Segmented, Sheet, SkeletonRows, cx } from '../components/ui';
import { fmtDate, fmtSize, fmtUsd, fmtWhen, parseTime, plural, tracksWord } from '../lib/format';
import { useAsync } from '../lib/hooks';
import { Link } from '../lib/router';
import { useSession } from '../lib/session';
import { EMBEDDED } from '../lib/embedded';
import { canShare, copyText, shareText } from '../lib/share';
import { attempt, confirmDialog, toast } from '../lib/ui';

const DAYS = [
  { value: '1', label: '1 день' },
  { value: '7', label: '7 дней' },
  { value: '30', label: '30 дней' },
];

export function AdminPage() {
  const users = useAsync((s) => api.adminUsers(s), []);
  const invites = useAsync((s) => api.invites(s), []);
  const storage = useAsync((s) => api.adminStorage(s), []);
  const config = useAsync((s) => api.adminConfig(s), []);
  const [creating, setCreating] = useState(false);
  const [shown, setShown] = useState<InviteOut>();

  const revoke = async (inv: InviteOut) => {
    const ok = await confirmDialog({
      title: 'Отозвать приглашение?',
      body: `Код ${inv.code}${inv.note ? ` (${inv.note})` : ''} перестанет действовать.`,
      okLabel: 'Отозвать',
      danger: true,
    });
    if (!ok) return;
    if (await attempt(() => api.deleteInvite(inv.id), 'Приглашение отозвано')) {
      invites.setData((prev) => prev?.filter((i) => i.id !== inv.id));
    }
  };

  const active = invites.data?.filter((i) => i.status === 'active') ?? [];
  const past = invites.data?.filter((i) => i.status !== 'active') ?? [];
  const d = users.data;

  return (
    <div className="pb-6">
      {/* in the app the page is opened from its settings: the system back leaves it */}
      <PageHeader title="Пользователи" back={EMBEDDED ? undefined : '/more'} right={<IconButton icon="refresh" label="Обновить" onClick={() => (users.reload(true), invites.reload(true), storage.reload(true), config.reload(true))} />} />

      <ul className="mx-4 mb-3 overflow-hidden rounded-xl bg-surface">
        <InsightLink to="/admin/insights" icon="history" title="Аналитика пользователей" sub="прослушивания, миксы, волна по пользователям" />
        <InsightLink to="/admin/taste" icon="heart" title="Веса песен у всех" sub="песни × пользователи по профилю вкуса" />
      </ul>

      {d && (
        <div className="grid grid-cols-3 gap-2 px-4 pb-2">
          <Tile label="ИИ за 30 дней" value={fmtUsd(d.ai_cost_30d_usd)} />
          <Tile label="ИИ всего" value={fmtUsd(d.ai_cost_usd)} />
          <Tile label="Песен всего" value={String(d.library_tracks)} />
        </div>
      )}

      {storage.data && <StorageBlock s={storage.data} />}
      {config.data && <ConfigBlock value={config.data} onSaved={(next) => config.setData(next)} />}

      <SectionTitle
        right={
          <Button small variant="primary" icon="plus" onClick={() => setCreating(true)}>
            Пригласить
          </Button>
        }
      >
        Приглашения
      </SectionTitle>
      {invites.loading ? (
        <SkeletonRows n={2} />
      ) : invites.error ? (
        <ErrorState text={invites.error} onRetry={() => invites.reload()} />
      ) : !active.length && !past.length ? (
        <p className="px-4 pb-2 text-sm text-muted">
          Новый человек входит по коду приглашения: создайте код и отправьте его. Код одноразовый.
        </p>
      ) : (
        <ul className="mx-4 overflow-hidden rounded-xl bg-surface">
          {active.map((inv) => (
            <li key={inv.id} className="flex min-h-16 items-center gap-2 border-b border-line/60 py-2 pl-4 pr-1 last:border-0">
              <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setShown(inv)}>
                <span className="block font-mono text-base font-semibold tracking-wider">{inv.code}</span>
                <span className="block truncate text-xs text-muted">
                  {[inv.note, `до ${fmtDate(inv.expires_at)}`].filter(Boolean).join(' · ')}
                </span>
              </button>
              <IconButton icon="send" label="Отправить код" onClick={() => setShown(inv)} />
              <IconButton icon="trash" label="Отозвать" onClick={() => revoke(inv)} />
            </li>
          ))}
          {past.map((inv) => (
            <li key={inv.id} className="flex min-h-14 items-center gap-3 border-b border-line/60 px-4 py-2 text-muted last:border-0">
              <span className="min-w-0 flex-1">
                <span className="block font-mono text-sm line-through">{inv.code}</span>
                <span className="block truncate text-xs">
                  {inv.status === 'used'
                    ? `${inv.note ? inv.note + ' · ' : ''}аккаунт ${inv.used_by ?? '—'} создан ${fmtWhen(inv.used_at)}`
                    : `${inv.note ? inv.note + ' · ' : ''}истёк ${fmtDate(inv.expires_at)}`}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      <SectionTitle>Пользователи</SectionTitle>
      {users.loading ? (
        <SkeletonRows n={3} />
      ) : users.error ? (
        <ErrorState text={users.error} onRetry={() => users.reload()} />
      ) : !d?.users.length ? (
        <Empty icon="user" title="Пользователей нет" />
      ) : (
        <ul className="space-y-2 px-4">
          {d.users.map((u) => (
            <li key={u.username}>
              <UserCard u={u} />
            </li>
          ))}
        </ul>
      )}

      <CreateInviteSheet
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={(inv) => {
          setCreating(false);
          invites.setData((prev) => [inv, ...(prev ?? [])]);
          setShown(inv);
        }}
      />
      <InviteSheet invite={shown} onClose={() => setShown(undefined)} />
    </div>
  );
}

function InsightLink({ to, icon, title, sub }: { to: string; icon: 'history' | 'heart'; title: string; sub: string }) {
  return (
    <li className="border-b border-line/60 last:border-0">
      <Link to={to} className="flex min-h-14 items-center gap-3 px-4 py-2 hover:bg-fg/5">
        <Icon name={icon} size={20} className="shrink-0 text-accent" />
        <span className="min-w-0 flex-1">
          <span className="block font-semibold">{title}</span>
          <span className="block truncate text-xs text-muted">{sub}</span>
        </span>
        <Icon name="chevron" size={18} className="shrink-0 text-dim" />
      </Link>
    </li>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl bg-surface px-3 py-2.5">
      <div className="truncate text-lg font-bold">{value}</div>
      <div className="truncate text-[11px] text-muted">{label}</div>
    </div>
  );
}

const ALBUM_WORD: [string, string, string] = ['альбом', 'альбома', 'альбомов'];

/** Диск и библиотека: занятое место, разбивка по каталогам и топ альбомов по размеру. */
function StorageBlock({ s }: { s: AdminStorageOut }) {
  const used = s.disk_total ? s.disk_used / s.disk_total : 0;
  const dirs: Array<[string, number]> = [
    ['Библиотека', s.library_bytes],
    ['Корзина', s.trash_bytes],
    ['Кеш прослушивания', s.listen_bytes],
    ['Opus-кеш', s.listen_light_bytes],
    ['Загрузки (staging)', s.uploads_bytes],
    ['Бэкапы', s.backups_bytes],
    ['yt-dlp', s.ytdlp_bytes],
    ['Ассистент', s.assistant_bytes],
  ];
  return (
    <>
      <SectionTitle>Диск и библиотека</SectionTitle>
      <div className="mx-4 rounded-xl bg-surface p-4">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-sm font-semibold">Занято на диске</span>
          <span className="text-sm text-muted">{Math.round(used * 100)}%</span>
        </div>
        <ProgressBar value={used} className="mt-2" />
        <p className="mt-1.5 text-xs text-muted">
          занято {fmtSize(s.disk_used)} из {fmtSize(s.disk_total)}, свободно {fmtSize(s.disk_free)}
        </p>
        <p className="mt-1 text-xs text-muted">Кеш на staging: {fmtSize(s.listen_bytes + s.listen_light_bytes)} / {fmtSize(s.cache_limit)} (мягкий лимит), свободно {fmtSize(s.cache_free)}</p>
        <p className="mt-1 text-xs text-muted">Автоскачивание прослушанного: {s.autodownload_listened ? 'включено' : 'выключено'}</p>
        <p className="mt-3 text-sm">
          Библиотека: {tracksWord(s.tracks)}, {s.albums} {plural(s.albums, ALBUM_WORD)}, {fmtSize(s.library_tracks_bytes)}
        </p>
        {s.tracks_without_size > 0 && (
          <Notice>У {tracksWord(s.tracks_without_size)} не известен размер (бэкфилл не прошёл)</Notice>
        )}
        <ul className="mt-3 divide-y divide-line/60 text-sm">
          {dirs.map(([label, bytes]) => (
            <li key={label} className="flex items-center justify-between py-1.5">
              <span className="text-muted">{label}</span>
              <span className="shrink-0 font-medium tabular-nums">{fmtSize(bytes)}</span>
            </li>
          ))}
        </ul>
        {s.largest_albums.length > 0 && (
          <div className="mt-4">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted">Топ по размеру</div>
            <ul className="mt-1">
              {s.largest_albums.map((a) => (
                <li key={a.id} className="border-b border-line/60 last:border-0">
                  <Link to={`/library/album/${a.id}`} className="flex min-h-11 items-center justify-between gap-2 py-1.5 text-sm hover:bg-fg/5">
                    <span className="min-w-0 truncate">
                      <span className="font-medium">{a.title}</span>
                      {a.album_artist ? <span className="text-muted"> — {a.album_artist}</span> : null}
                    </span>
                    <span className="shrink-0 tabular-nums text-muted">{fmtSize(a.size_bytes)}</span>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </>
  );
}

function ConfigBlock({ value, onSaved }: { value: AdminConfigOut; onSaved: (next: AdminConfigOut) => void }) {
  const [form, setForm] = useState(value);
  const [busy, setBusy] = useState(false);
  const update = (key: keyof AdminConfigOut, raw: string | boolean) => setForm((prev) => ({ ...prev, [key]: typeof raw === 'boolean' ? raw : Number(raw) }));
  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const { restart_required: _, ...body } = form;
    const next = await attempt(() => api.updateAdminConfig(body), 'Настройки сохранены');
    setBusy(false);
    if (next) onSaved(next);
  };
  return (
    <>
      <SectionTitle>Настройки хранения</SectionTitle>
      <form onSubmit={save} className="mx-4 space-y-3 rounded-xl bg-surface p-4">
        <ConfigNumber label="Корзина, дней" value={form.trash_days} onChange={(v) => update('trash_days', v)} />
        <ConfigNumber label="Кеш прослушивания, часов" value={Math.round(form.listen_keep_s / 3600)} onChange={(v) => update('listen_keep_s', String(Number(v) * 3600))} />
        <ConfigNumber label="Общий лимит кеша, МБ" value={form.listen_max_mb} onChange={(v) => update('listen_max_mb', v)} />
        <ConfigNumber label="Минимум свободного места, МБ" value={form.listen_min_free_mb} onChange={(v) => update('listen_min_free_mb', v)} />
        <ConfigNumber label="Кеш yt-dlp, дней" value={form.ytdlp_cache_days} onChange={(v) => update('ytdlp_cache_days', v)} />
        <ConfigNumber label="Картинки ассистента, дней" value={form.assistant_days} onChange={(v) => update('assistant_days', v)} />
        <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" className="h-5 w-5 accent-[#1ed760]" checked={form.autodownload_listened} onChange={(e) => update('autodownload_listened', e.target.checked)} /><span>Сохранять прослушанные треки в библиотеку</span></label>
        <p className="text-xs text-muted">После сохранения перезапустите Hub, чтобы новые значения вступили в силу.</p>
        <Button type="submit" variant="primary" block busy={busy}>Сохранить настройки</Button>
      </form>
    </>
  );
}

function ConfigNumber({ label, value, onChange }: { label: string; value: number; onChange: (value: string) => void }) {
  return <Field label={label}><input className="input" type="number" min={0} value={value} onChange={(e) => onChange(e.target.value)} /></Field>;
}

/** The latest of the Hub's and the player's last activity. */
export function lastActivity(u: AdminUserOut): string | null {
  const times = [u.last_seen_at, u.player_seen_at].filter((t): t is string => !!t);
  if (!times.length) return null;
  return times.reduce((a, b) => (parseTime(a) > parseTime(b) ? a : b));
}

export function UserBadges({ u }: { u: AdminUserOut }) {
  const badges: ReactNode[] = [];
  if (u.is_admin) badges.push(<Badge key="a">Админ</Badge>);
  if (u.google_email) badges.push(<Badge key="g">Google</Badge>);
  if (u.email) badges.push(<Badge key="e">почта</Badge>);
  if (!u.in_hub) badges.push(<Badge key="p">только плеер</Badge>);
  return <>{badges}</>;
}

function Badge({ children }: { children: ReactNode }) {
  return <span className="shrink-0 rounded-full bg-raised px-2 py-0.5 text-[11px] font-semibold text-muted">{children}</span>;
}

function UserCard({ u }: { u: AdminUserOut }) {
  const seen = lastActivity(u);
  const body = (
    <>
      <div className="flex items-center gap-3">
        <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-accent font-bold uppercase text-on-accent">{u.username.slice(0, 1)}</span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="truncate font-semibold">{u.username}</span>
            <UserBadges u={u} />
          </span>
          <span className="block truncate text-xs text-muted">
            {[u.name && u.name !== u.username ? u.name : null, seen ? `был(а) ${fmtWhen(seen)}` : 'ещё не заходил(а)'].filter(Boolean).join(' · ')}
          </span>
        </span>
        {u.id != null && <Icon name="chevron" size={18} className="shrink-0 text-dim" />}
      </div>
      {u.in_hub && (
        <div className="mt-3 grid grid-cols-3 gap-2 text-center">
          <Stat value={fmtUsd(u.ai_cost_usd)} label={`ИИ · ${u.ai_requests} ${plural(u.ai_requests, ['запрос', 'запроса', 'запросов'])}`} />
          <Stat value={String(u.downloads_done)} label={plural(u.downloads_done, ['песня скачана', 'песни скачано', 'песен скачано'])} />
          <Stat value={u.starred_songs == null ? '—' : String(u.starred_songs)} label="♥ понравилось" />
        </div>
      )}
    </>
  );
  const cls = 'block rounded-xl bg-surface p-3';
  return u.id != null ? (
    <Link to={`/admin/user/${u.id}`} className={cx(cls, 'hover:bg-fg/5')}>
      {body}
    </Link>
  ) : (
    <div className={cls}>{body}</div>
  );
}

export function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="min-w-0 rounded-lg bg-raised px-1 py-2">
      <div className="truncate font-semibold">{value}</div>
      <div className="truncate text-[11px] text-muted">{label}</div>
    </div>
  );
}

function CreateInviteSheet({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (inv: InviteOut) => void }) {
  const [note, setNote] = useState('');
  const [days, setDays] = useState('7');
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const inv = await attempt(() => api.createInvite({ note: note.trim(), days: Number(days) }));
    setBusy(false);
    if (inv) {
      setNote('');
      onCreated(inv);
    }
  };

  return (
    <Sheet open={open} onClose={onClose} title="Новое приглашение">
      <form onSubmit={submit} className="space-y-4 pb-2">
        <Field label="Для кого">
          <input className="input" placeholder="Например, Анна" maxLength={128} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <div>
          <span className="label">Код действует</span>
          <Segmented label="Срок действия" value={days} options={DAYS} onChange={setDays} />
        </div>
        <p className="text-xs text-muted">Код одноразовый: по нему человек сам выберет имя и пароль. Имя «Для кого» станет его отображаемым именем.</p>
        <Button type="submit" variant="primary" block busy={busy}>
          Создать код
        </Button>
      </form>
    </Sheet>
  );
}

function inviteMessage(inv: InviteOut, hubUrl: string): string {
  const apk = `${hubUrl.replace(/\/$/, '')}/app/latest.apk`;
  return [
    `Приглашение в HomeMusic${inv.note ? ` для: ${inv.note}` : ''}.`,
    `1. Установите приложение: ${apk}`,
    '2. На экране входа нажмите «Далее», затем «Регистрация по коду».',
    `3. Введите код ${inv.code} и придумайте имя и пароль.`,
    `Код действует до ${fmtDate(inv.expires_at)}.`,
  ].join('\n');
}

function InviteSheet({ invite, onClose }: { invite?: InviteOut; onClose: () => void }) {
  const { me } = useSession();
  if (!invite) return null;
  const text = inviteMessage(invite, me.public_hub_url || window.location.origin);
  const copy = async (what: string, done: string) => toast((await copyText(what)) ? done : 'Не удалось скопировать', 'success');
  return (
    <Sheet open onClose={onClose} title="Код приглашения">
      <div className="space-y-4 pb-2 text-center">
        <div className="rounded-xl bg-raised py-5 font-mono text-3xl font-bold tracking-[0.2em]">{invite.code}</div>
        <p className="text-sm text-muted">
          {invite.note ? `Для: ${invite.note}. ` : ''}Действует до {fmtDate(invite.expires_at)}, один раз.
        </p>
        <pre className="whitespace-pre-wrap rounded-lg bg-bg p-3 text-left text-xs text-muted">{text}</pre>
        <div className="flex gap-2">
          {canShare() && (
            <Button variant="primary" icon="send" block onClick={() => void shareText(text)}>
              Отправить
            </Button>
          )}
          <Button icon="paste" block onClick={() => void copy(text, 'Сообщение скопировано')}>
            Скопировать
          </Button>
        </div>
        <button type="button" className="min-h-11 text-sm font-semibold text-accent" onClick={() => void copy(invite.code, 'Код скопирован')}>
          Скопировать только код
        </button>
      </div>
    </Sheet>
  );
}
