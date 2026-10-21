"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Aperture,
  ArrowDown,
  ArrowLeft,
  ArrowUpRight,
  Check,
  ChevronRight,
  Database,
  ExternalLink,
  Grid2X2,
  Image as ImageIcon,
  LayoutGrid,
  LoaderCircle,
  MapPin,
  MessageCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  X,
  Camera,
  Play,
  CloudOff,
} from "lucide-react";
import type { Detail, Media, Page, Profile, Summary } from "../lib/types";
import { safeURL, mediaKind } from "../lib/types";
import ArchiveMap from "./map";
import { snapshotAPI } from "../lib/snapshot-client";

type View = "gallery" | "profiles" | "map";
type Failure = { code: string; message: string };
const number = (value?: number) =>
  value === undefined ? "—" : new Intl.NumberFormat("ru-RU").format(value);
function date(value: string) {
  if (!value) return "Дата не указана";
  const d = new Date(value);
  return isNaN(d.getTime())
    ? value
    : d.toLocaleDateString("ru-RU", {
        day: "numeric",
        month: "long",
        year: "numeric",
      });
}
const api = snapshotAPI;
function Thumbnail({
  url,
  alt,
  large = false,
}: {
  url: string;
  alt: string;
  large?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const src = safeURL(url);
  if (!src || failed)
    return (
      <div className="image-fallback">
        <ImageIcon size={large ? 40 : 26} />
        <span>Превью недоступно</span>
      </div>
    );
  return mediaKind(url) === "video" ? (
    <video
      src={src}
      preload="metadata"
      muted
      playsInline
      onError={() => setFailed(true)}
    />
  ) : (
    <img
      src={src}
      alt={alt}
      loading={large ? "eager" : "lazy"}
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  );
}
export default function Archive({ onLock }: { onLock?: () => void }) {
  const [view, setView] = useState<View>("gallery");
  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [exact, setExact] = useState(false);
  const [chat, setChat] = useState("");
  const [geo, setGeo] = useState(false);
  const [compact, setCompact] = useState(false);
  const [stats, setStats] = useState<Summary>();
  const [items, setItems] = useState<Media[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [limited, setLimited] = useState(false);
  const [busy, setBusy] = useState(true);
  const [moreBusy, setMoreBusy] = useState(false);
  const [error, setError] = useState<Failure>();
  const [refresh, setRefresh] = useState(0);
  const [updated, setUpdated] = useState<string>("");
  const [selected, setSelected] = useState<Media | null>(null);
  const generation = useRef(0);
  const moreController = useRef<AbortController | null>(null);
  const params = useCallback(
    () =>
      new URLSearchParams({
        view: view === "gallery" ? "media" : view,
        q: search,
        chat,
        geo: geo ? "1" : "0",
        exact: exact ? "1" : "0",
      }),
    [view, search, chat, geo, exact],
  );
  useEffect(() => {
    const timer = setTimeout(() => setSearch(q.trim()), 350);
    return () => clearTimeout(timer);
  }, [q]);
  useEffect(() => {
    const controller = new AbortController();
    const gen = ++generation.current;
    moreController.current?.abort();
    setMoreBusy(false);
    setBusy(true);
    setError(undefined);
    setNext(null);
    setItems([]);
    setProfiles([]);
    api<{
      items: Media[] | Profile[];
      next?: string | null;
      limited?: boolean;
    }>(params(), controller.signal)
      .then((data) => {
        if (gen !== generation.current) return;
        if (view === "profiles") setProfiles(data.items as Profile[]);
        else setItems(data.items as Media[]);
        setNext(data.next || null);
        setLimited(!!data.limited);
        setUpdated(
          new Date().toLocaleTimeString("ru-RU", {
            hour: "2-digit",
            minute: "2-digit",
          }),
        );
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e);
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
    return () => controller.abort();
  }, [params, refresh, view]);
  useEffect(() => {
    const controller = new AbortController();
    api<Summary>(new URLSearchParams({ view: "summary" }), controller.signal)
      .then(setStats)
      .catch(() => {});
    return () => controller.abort();
  }, [refresh]);
  async function loadMore() {
    if (!next || moreBusy) return;
    const gen = generation.current;
    const controller = new AbortController();
    moreController.current = controller;
    setMoreBusy(true);
    setError(undefined);
    const p = params();
    p.set("cursor", next);
    try {
      const data = await api<Page | { items: Profile[]; next: string | null }>(
        p,
        controller.signal,
      );
      if (gen !== generation.current) return;
      if (view === "profiles")
        setProfiles((previous) => [...previous, ...(data.items as Profile[])]);
      else setItems((previous) => [...previous, ...(data.items as Media[])]);
      setNext(data.next);
    } catch (e) {
      if (!controller.signal.aborted) setError(e as Failure);
    } finally {
      if (gen === generation.current) setMoreBusy(false);
    }
  }
  const reset = () => {
    setExact(false);
    setQ("");
    setSearch("");
    setChat("");
    setGeo(false);
  };
  const filtered = !!(search || chat || geo);

  const shown = view === "profiles" ? profiles.length : items.length;
  const title =
    view === "gallery"
      ? "Галерея"
      : view === "profiles"
        ? "Профили"
        : "На карте";
  return (
    <div className="site-shell">
      <header className="topbar">
        <a
          className="brand"
          href={import.meta.env.BASE_URL}
          aria-label="VSCO Archive — главная"
        >
          <span className="brand-icon">
            <Aperture size={24} />
          </span>
          <span>
            VSCO<span className="brand-light"> / Archive</span>
          </span>
        </a>
        <span className="private-tag">
          <ShieldCheck size={14} /> Приватный архив
        </span>
        {onLock && (
          <button className="lock-button" onClick={onLock}>
            Выйти
          </button>
        )}
      </header>
      <main>
        <section className="hero">
          <div>
            <div className="eyebrow">
              <span /> ВАША ВИЗУАЛЬНАЯ БИБЛИОТЕКА
            </div>
            <h1>
              Моменты остаются.
              <br />
              <span>Архив всегда рядом.</span>
            </h1>
            <p>
              Фотографии, профили и места из вашего VSCO-архива.
              <br className="desktop-break" /> Всё в одном пространстве — с
              данными из зашифрованного снимка архива.
            </p>
          </div>
          <div className="connection-card">
            <div className={"connection-icon " + (error ? "inactive" : "")}>
              <Database size={22} />
            </div>
            <div>
              <span className="connection-label">ИСТОЧНИК ДАННЫХ</span>
              <strong>Neon → GitHub Pages</strong>
              <span className="connection-state">
                <i
                  className={busy ? "pending" : error ? "offline" : "online"}
                />
                {busy
                  ? "Загружаем снимок"
                  : error
                    ? "Снимок недоступен"
                    : "Снимок открыт"}
              </span>
            </div>
            <span className="connection-line" />
          </div>
        </section>
        <section className="stats-row" aria-label="Статистика всего архива">
          {[
            { icon: ImageIcon, value: stats?.items, label: "медиафайлов" },
            { icon: Users, value: stats?.profiles, label: "профилей с медиа" },
            { icon: MapPin, value: stats?.geo, label: "с координатами" },
            {
              icon: MessageCircle,
              value: stats?.comments,
              label: "комментариев",
            },
          ].map(({ icon: Icon, value, label }) => (
            <div className="stat" key={label}>
              <span className="stat-icon">
                <Icon size={19} />
              </span>
              <div>
                <strong>{number(value)}</strong>
                <span>{label}</span>
              </div>
            </div>
          ))}
        </section>
        <section className="archive-section" aria-label="Просмотр архива">
          <div className="section-top">
            <nav className="view-tabs" aria-label="Вид архива">
              {(
                [
                  { key: "gallery", label: "Галерея", icon: LayoutGrid },
                  { key: "profiles", label: "Профили", icon: Users },
                  { key: "map", label: "Карта", icon: MapPin },
                ] as const
              ).map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  className={view === key ? "active" : ""}
                  onClick={() => setView(key)}
                  aria-pressed={view === key}
                >
                  <Icon size={17} />
                  {label}
                </button>
              ))}
            </nav>
            <button
              className="refresh-button"
              onClick={() => setRefresh((n) => n + 1)}
              disabled={busy}
              aria-label="Обновить архив"
            >
              <RefreshCw size={15} className={busy ? "spin" : ""} />
              <span>Обновить</span>
            </button>
          </div>
          <div className="filters">
            <label className="search-field">
              <Search size={18} />
              <input
                placeholder="Найти профиль по имени…"
                value={q}
                onChange={(e) => {
                  setExact(false);
                  setQ(e.target.value);
                }}
                aria-label="Поиск по имени профиля"
              />
              {q && (
                <button
                  onClick={() => {
                    setExact(false);
                    setQ("");
                    setSearch("");
                  }}
                  aria-label="Очистить поиск"
                >
                  <X size={16} />
                </button>
              )}
            </label>
            <span className="filter-divider" />
            <label className="chat-filter">
              <SlidersHorizontal size={16} />
              <select
                value={chat}
                onChange={(e) => setChat(e.target.value)}
                aria-label="Фильтр по Telegram-чату"
              >
                <option value="">Все чаты</option>
                {stats?.chats.map((c) => (
                  <option key={c.chat_id} value={c.chat_id}>
                    Чат {c.chat_id} · {number(c.count)}
                  </option>
                ))}
              </select>
            </label>
            <label className="geo-filter">
              <input
                type="checkbox"
                checked={geo}
                onChange={(e) => setGeo(e.target.checked)}
              />
              <span>
                <Check size={11} />
              </span>
              С координатами
            </label>
          </div>
          <div className="results-heading">
            <div>
              <h2>{search ? "@" + search : title}</h2>
              <span>
                {busy
                  ? "Загрузка…"
                  : error && !shown
                    ? "Данные недоступны"
                    : view === "map"
                      ? `${limited ? "Последние " : ""}${number(shown)} точек`
                      : `Показано ${number(shown)}`}
              </span>
              {filtered && (
                <button className="clear-filters" onClick={reset}>
                  Сбросить фильтры <X size={12} />
                </button>
              )}
            </div>
            <div className="results-meta">
              {updated && <span>Обновлено в {updated}</span>}
              {view === "gallery" && (
                <div className="density">
                  <button
                    className={!compact ? "active" : ""}
                    onClick={() => setCompact(false)}
                    aria-label="Крупные карточки"
                    aria-pressed={!compact}
                  >
                    <LayoutGrid size={16} />
                  </button>
                  <button
                    className={compact ? "active" : ""}
                    onClick={() => setCompact(true)}
                    aria-label="Компактные карточки"
                    aria-pressed={compact}
                  >
                    <Grid2X2 size={16} />
                  </button>
                </div>
              )}
            </div>
          </div>
          <div aria-live="polite">
            {error && !shown ? (
              <div className="empty-state">
                <CloudOff size={34} />
                <h3>Архив пока недоступен</h3>
                <p>{error.message}</p>
                <button
                  className="primary-button"
                  onClick={() => setRefresh((n) => n + 1)}
                >
                  Повторить подключение <RefreshCw size={15} />
                </button>
              </div>
            ) : busy ? (
              <div
                className={
                  "media-grid skeleton-grid " + (compact ? "compact" : "")
                }
                aria-label="Загрузка архива"
              >
                {Array.from({ length: 8 }, (_, i) => (
                  <div className="skeleton-card" key={i}>
                    <div />
                    <span />
                    <span />
                  </div>
                ))}
              </div>
            ) : !shown ? (
              <div className="empty-state">
                <Search size={32} />
                <h3>
                  {filtered
                    ? "Ничего не найдено"
                    : view === "map"
                      ? "Нет записей с координатами"
                      : "В архиве пока нет записей"}
                </h3>
                <p>
                  {filtered
                    ? "Попробуйте другое имя или уберите часть фильтров."
                    : "Записи появятся здесь после сохранения данных ботом."}
                </p>
                {filtered && (
                  <button className="primary-button" onClick={reset}>
                    Сбросить фильтры <ArrowUpRight size={16} />
                  </button>
                )}
              </div>
            ) : view === "map" ? (
              <>
                <ArchiveMap items={items} onSelect={setSelected} />
                <p className="map-note">
                  <MapPin size={14} />
                  {limited
                    ? "Показаны 2 000 последних записей с координатами. Используйте поиск или фильтр чата, чтобы сузить выборку."
                    : "Нажмите на точку, чтобы открыть запись. Некоторые фотографии могут иметь одинаковые координаты."}
                </p>
              </>
            ) : view === "profiles" ? (
              <div className="profile-grid">
                {profiles.map((p) => (
                  <button
                    className="profile-card"
                    key={p.latest}
                    onClick={() => {
                      setExact(true);
                      setQ(p.username);
                      setSearch(p.username);
                      setView("gallery");
                    }}
                  >
                    <div className="profile-cover">
                      <Thumbnail
                        url={p.image_url}
                        alt={"Фото профиля " + p.username}
                      />
                      <span className="profile-cover-shade" />
                      <span className="profile-avatar">
                        {(p.username || "?")[0].toUpperCase()}
                      </span>
                    </div>
                    <div className="profile-body">
                      <div>
                        <h3>@{p.username || "Без имени"}</h3>
                        <p>{number(p.count)} медиафайлов</p>
                      </div>
                      <span className="profile-arrow">
                        <ArrowUpRight size={18} />
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className={"media-grid " + (compact ? "compact" : "")}>
                {items.map((item) => (
                  <button
                    className="media-card"
                    key={item.id}
                    onClick={() => setSelected(item)}
                  >
                    <div className="media-image">
                      <Thumbnail
                        url={item.image_url}
                        alt={"Фото @" + item.username}
                      />
                      <span className="media-shade" />
                      <span className="image-open">
                        <ArrowUpRight size={19} />
                      </span>
                      {item.latitude !== null && item.longitude !== null && (
                        <span className="geo-badge">
                          <MapPin size={12} /> Геометка
                        </span>
                      )}
                      {mediaKind(item.image_url) === "video" && (
                        <span className="video-badge">
                          <Play size={14} /> Видео
                        </span>
                      )}
                    </div>
                    <div className="media-caption">
                      <span className="avatar">
                        {(item.username || "?")[0].toUpperCase()}
                      </span>
                      <div>
                        <h3>@{item.username || "Без имени"}</h3>
                        <span>{date(item.created_at)}</span>
                      </div>
                      <ChevronRight size={16} />
                    </div>
                  </button>
                ))}
              </div>
            )}
            {error && !!shown && (
              <div className="inline-error">
                {error.message}
                <button onClick={loadMore}>Повторить</button>
              </div>
            )}
          </div>
          {!busy && next && (
            <div className="load-more">
              <button
                className="load-more-button"
                onClick={loadMore}
                disabled={moreBusy}
              >
                {moreBusy ? (
                  <LoaderCircle size={17} className="spin" />
                ) : (
                  <ArrowDown size={17} />
                )}{" "}
                {moreBusy ? "Загружаем…" : "Показать ещё"}
                <span>
                  {view === "profiles" ? "36 профилей" : "48 записей"}
                </span>
              </button>
            </div>
          )}
        </section>
        <section className="archive-note">
          <span className="note-icon">
            <Aperture size={20} />
          </span>
          <div>
            <strong>Архив на GitHub Pages</strong>
            {stats?.snapshot_at && (
              <p>
                Снимок от {new Date(stats.snapshot_at).toLocaleString("ru-RU")}
              </p>
            )}
            <p>
              Бот сохраняет записи в Neon. GitHub Actions обновляет снимок
              каждые 6 часов или по ручному запуску. «Обновить» проверяет
              последнюю публикацию.
            </p>
          </div>
          <span className="read-only">
            <ShieldCheck size={14} /> Только просмотр
          </span>
        </section>
      </main>
      <footer>
        <a className="footer-brand" href={import.meta.env.BASE_URL}>
          <Aperture size={17} /> VSCO / Archive
        </a>
        <span>Сохранённые моменты. Новые перспективы.</span>
        <span>Онлайн-библиотека</span>
      </footer>
      {selected && (
        <DetailDialog item={selected} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function DetailDialog({ item, onClose }: { item: Media; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [detail, setDetail] = useState<Detail>();
  const [error, setError] = useState("");
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement as HTMLElement;
    element?.showModal();
    const before = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = before;
      previous?.focus();
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    api<Detail>(
      new URLSearchParams({ view: "detail", id: item.id }),
      controller.signal,
    )
      .then(setDetail)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      });
    return () => controller.abort();
  }, [item.id]);
  const profile =
    safeURL(item.profile_url) ||
    "https://vsco.co/" + encodeURIComponent(item.username);
  const image = safeURL(item.image_url);
  const coords = item.latitude !== null && item.longitude !== null;
  return (
    <dialog
      ref={dialog}
      className="detail-dialog"
      aria-labelledby="detail-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === dialog.current) onClose();
      }}
    >
      <div className="detail-layout">
        <div className="detail-photo">
          {mediaKind(item.image_url) === "video" && image ? (
            <video src={image} controls autoPlay playsInline />
          ) : (
            <Thumbnail
              url={item.image_url}
              alt={"Фото @" + item.username}
              large
            />
          )}
          <button
            className="detail-back"
            onClick={onClose}
            aria-label="Закрыть просмотр"
          >
            <ArrowLeft size={18} />
            <span>В галерею</span>
          </button>
        </div>
        <div className="detail-sidebar">
          <button
            className="detail-close"
            onClick={onClose}
            aria-label="Закрыть"
          >
            <X size={20} />
          </button>
          <div className="detail-author">
            <span className="avatar large">
              {(item.username || "?")[0].toUpperCase()}
            </span>
            <span className="eyebrow">ИЗ ВАШЕГО АРХИВА</span>
            <h2 id="detail-title">@{item.username || "Без имени"}</h2>
            <a href={profile} target="_blank" rel="noreferrer">
              Открыть профиль VSCO <ArrowUpRight size={14} />
            </a>
          </div>
          <dl className="detail-facts">
            <div>
              <dt>Добавлено в архив</dt>
              <dd>{date(item.created_at)}</dd>
            </div>
            {detail?.camera && (
              <div>
                <dt>
                  <Camera size={13} /> Камера
                </dt>
                <dd>{detail.camera}</dd>
              </div>
            )}
            {coords && (
              <div>
                <dt>
                  <MapPin size={13} /> Координаты
                </dt>
                <dd>
                  <a
                    href={`https://www.openstreetmap.org/?mlat=${item.latitude}&mlon=${item.longitude}#map=14/${item.latitude}/${item.longitude}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {item.latitude?.toFixed(5)}, {item.longitude?.toFixed(5)}{" "}
                    <ExternalLink size={12} />
                  </a>
                </dd>
              </div>
            )}
            <div>
              <dt>Telegram-чат</dt>
              <dd>{item.chat_id}</dd>
            </div>
            {detail?.source && (
              <div>
                <dt>Источник</dt>
                <dd>{detail.source}</dd>
              </div>
            )}
          </dl>
          <div className="comments">
            <h3>
              <MessageCircle size={16} /> Комментарии{" "}
              {detail && <span>{detail.comments.length}</span>}
            </h3>
            {error ? (
              <p className="detail-error">{error}</p>
            ) : !detail ? (
              <p className="muted">Загрузка комментариев…</p>
            ) : detail.comments.length ? (
              detail.comments.map((c) => (
                <div className="comment" key={c.id}>
                  <p>{c.comment}</p>
                  <time>{date(c.created_at)}</time>
                </div>
              ))
            ) : (
              <p className="muted">Нет сохранённых комментариев.</p>
            )}
          </div>
          {image && (
            <a
              className="original-link"
              href={image}
              target="_blank"
              rel="noreferrer"
            >
              Открыть оригинал <ArrowUpRight size={17} />
            </a>
          )}
          <p className="detail-hint">
            Изображения загружаются с сервера VSCO. Если превью недоступно,
            попробуйте открыть оригинал или профиль.
          </p>
        </div>
      </div>
    </dialog>
  );
}
