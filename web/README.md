# Онлайн-архив VSCO: GitHub Actions → Cloudflare Workers → Neon

Та же галерея, профили, комментарии и карта, что в версии VSCO Archive для Sites. Эта версия хранится в репозитории AFK и публикуется GitHub Actions в **ваш аккаунт Cloudflare**. База остаётся в Neon, сайт читает таблицы `items`, `links`, `comments` без импорта и изменения схемы. Telegram-бот работает отдельно.

GitHub Actions выполняет проверки, сборку и публикацию. HTTP-запросы пользователей обслуживает Cloudflare Worker, а не временный runner Actions. Фотографии загружаются по сохранённым URL VSCO; файлы, оставшиеся только на локальном диске, автоматически не загружаются.

## Первый запуск

1. Создайте бесплатный аккаунт Cloudflare, если его ещё нет. В **Workers & Pages** активируйте поддомен `*.workers.dev`. Собственный домен не обязателен.
2. В Cloudflare создайте API token с шаблоном **Edit Cloudflare Workers** для нужного аккаунта. Ему нужны Account → Workers Scripts → Edit и Account Settings → Read. Для размещения на `workers.dev` не требуются права на DNS или маршруты зон. Account ID находится на странице аккаунта / Workers & Pages. Не используйте Global API Key.
3. В GitHub откройте **StLisovafk13/AFK → Settings → Secrets and variables → Actions → Secrets**. Добавьте значения в Repository secrets или в GitHub Environment `archive-production`:

   | Secret | Значение |
   | --- | --- |
   | `CLOUDFLARE_API_TOKEN` | Созданный токен Cloudflare |
   | `CLOUDFLARE_ACCOUNT_ID` | ID вашего аккаунта Cloudflare |
   | `DATABASE_URL` | Строка подключения из Neon Connect к уже импортированной базе |
   | `ARCHIVE_PASSWORD` | Уникальный случайный пароль сайта, не менее 20 символов |

   Если `DATABASE_URL` уже есть в Repository secrets, используйте его. Секрет из настроек Sites автоматически сюда не переносится. Для сайта желательно использовать отдельную роль Neon с `CONNECT` к базе, `USAGE` на схему и `SELECT` на три таблицы. Не добавляйте секреты в `.env` репозитория, workflow, исходники или `VITE_*`.

4. При желании добавьте **Actions → Variables**: `ARCHIVE_USERNAME` (по умолчанию `archive`, 1–64 латинских буквы, цифры, `_`, `.` или `-`) и `ARCHIVE_WORKER_NAME` (по умолчанию `afk-vsco-archive`). Выберите новое имя Worker, чтобы не заменить другой существующий сайт в вашем аккаунте.
5. Слейте PR в `main`. Workflow **Online archive website** выполнит проверки и опубликует сайт. Также можно открыть **Actions → Online archive website → Run workflow → main**. Публикация с других веток и из PR отключена.
6. В шаге **Deploy Worker, static assets and runtime secrets together** появится URL `https://<имя-worker>.<ваш-поддомен>.workers.dev`. Откройте его: браузер запросит логин `archive` и заданный `ARCHIVE_PASSWORD`. Затем появятся фотографии и статистика из Neon.

Не нужно передавать пароли или токены в чат. Workflow берёт их из GitHub Secrets и передаёт в Cloudflare как **runtime secrets** вместе с новой версией Worker. Временный файл имеет закрытые права и удаляется после попытки публикации; в сборку frontend и артефакты значения не попадают.

В GitHub Environment `archive-production` можно включить защиту публикации и required reviewers. Это настройка владельца репозитория; проверки PR не используют это окружение и не требуют production-секретов.

## Как это работает

```text
Изменения web/ → GitHub Actions → тесты и сборка → Cloudflare Worker
                                                        ↓
Браузер → вход по логину и паролю → интерфейс / API → Neon PostgreSQL
                                                        ↑
                                                Telegram-бот
```

Интерфейс загружается как статические React/Vite assets. Worker проверяет HTTP Basic authentication по HTTPS **для всех маршрутов, включая assets**, а затем обслуживает файлы или выполняет SELECT через HTTP-драйвер Neon. Не отключайте `assets.run_worker_first`: иначе статические файлы смогут обходить проверку входа. Без сильного пароля Worker возвращает 503, без входа — 401. Поддельные заголовки идентификации не дают доступа.

Внешний сайт использует собственные реквизиты входа и не зависит от входа через ChatGPT/Sites. Все вошедшие с этим общим логином видят весь подключённый архив. Фильтр Telegram-чата не является системой разграничения прав бота. Сайт ничего не удаляет и не скачивает через Playwright.

Галерея читает 48 записей за запрос с курсором по ID; каталог — 36 профилей; карта — до 2 000 последних точек с координатами. Комментарии в карточке ограничены 200, метаданные для камеры — 128 КиБ. Статистика кэшируется в Worker на 60 секунд. «Обновить» читает новые записи бота без пересборки и без постоянного фонового опроса.

Бесплатные планы Cloudflare Workers и Neon имеют квоты запросов, CPU, вычислений и хранения. Конкретные актуальные лимиты смотрите в панели аккаунта; публикация не включает платный тариф автоматически. Ошибка 1102 означает превышение ресурса Worker, 429 может означать лимит запросов.

## Локально

Нужен Node.js 22.13+.

```sh
cd web
npm ci
```

Скопируйте `.dev.vars.example` в игнорируемый `.dev.vars`, заполните `DATABASE_URL`, `ARCHIVE_PASSWORD` и при желании `ARCHIVE_USERNAME`. `npm run dev` собирает frontend и запускает Worker на `http://127.0.0.1:8787`. Для локального адреса доступен HTTP; опубликованный адрес использует HTTPS. После изменения frontend перезапустите эту команду, чтобы пересобрать assets.

```sh
npm run check
npm test
npm run build
```

`npm run build` собирает frontend и выполняет Wrangler `--dry-run`, без публикации и без production-секретов. `npm run deploy` предназначена для настроенного Actions job. Публикуется отдельный Worker, текущая версия сайта в Sites остаётся доступной по прежнему адресу.

## Если публикация не прошла

- **Добавьте GitHub Actions secrets**: проверьте имена секретов и доступность выбранному Environment. Скрипт указывает только отсутствующие имена, значения не печатает.
- **Cloudflare authentication / authorization**: проверьте токен, Account ID, права Workers Scripts/Edit и привязку токена к аккаунту.
- **workers.dev disabled**: активируйте поддомен аккаунта в Cloudflare.
- **AUTH_NOT_CONFIGURED / 503**: пароль должен быть не короче 20 символов, логин — в допустимом формате.
- **DATABASE_UNAVAILABLE**: проверьте подключение к правильной Neon branch/database и права SELECT на `items`, `links`, `comments`.
- **Статистика есть, превью не видно**: изображения читаются браузером с исходных серверов VSCO. Доступность этих URL зависит от VSCO и сети посетителя; открытие оригинала/профиля доступно в карточке.

После исправления секретов запустите workflow вручную из `main`. Для изменения данных в Neon новая публикация не нужна.
