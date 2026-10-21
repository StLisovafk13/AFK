import { useState } from "react";
import { Aperture, LockKeyhole, ArrowRight, LoaderCircle } from "lucide-react";
import Archive from "./archive";
import { unlockSnapshot, lockSnapshot } from "../lib/snapshot-client";
export default function Login() {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (open)
    return (
      <Archive
        onLock={() => {
          lockSnapshot();
          setOpen(false);
        }}
      />
    );
  async function unlock(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await unlockSnapshot(password);
      setPassword("");
      setOpen(true);
    } catch (error) {
      setError(
        error instanceof Error ? error.message : "Не удалось открыть архив.",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <a className="brand" href={import.meta.env.BASE_URL}>
        <span className="brand-icon">
          <Aperture size={24} />
        </span>
        VSCO<span className="brand-light"> / Archive</span>
      </a>
      <form className="login-card" onSubmit={unlock}>
        <span className="setup-icon">
          <LockKeyhole size={28} />
        </span>
        <div className="eyebrow">ВАША ВИЗУАЛЬНАЯ БИБЛИОТЕКА</div>
        <h1>Откройте свой архив</h1>
        <p>
          Галерея, профили и места — в одном пространстве.
          <br />
          Введите пароль, чтобы расшифровать данные.
        </p>
        <label htmlFor="archive-password">Пароль архива</label>
        <input
          id="archive-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          disabled={busy}
        />
        {error && (
          <p className="login-error" role="alert">
            {error}
          </p>
        )}
        <button className="primary-button" disabled={busy || !password}>
          {busy ? (
            <LoaderCircle className="spin" size={17} />
          ) : (
            <ArrowRight size={17} />
          )}{" "}
          {busy ? "Открываем…" : "Открыть архив"}
        </button>
        <p className="login-note">
          Пароль используется в этом браузере и не отправляется на сервер.
          Данные обновляются через GitHub Actions.
        </p>
      </form>
    </main>
  );
}
