# VSCO Automation Toolkit

Эта коллекция скриптов и модулей помогает сканировать публичные профили VSCO, собирать ссылки на медиа, выгружать CSV/HTML отчёты и обслуживать базу данных бота. README объединяет справочник по окружению, отдельным скриптам и функциям, чтобы можно было быстро понять доступные возможности и их параметры.

## Подготовка окружения

1. Установите Python 3.11+.
2. Установите зависимости. Если файла `requirements.txt` нет, используйте минимальный набор:
   ```bash
   pip install playwright aiohttp beautifulsoup4 aiogram openpyxl pandas
   ```
3. Установите браузеры Playwright (используется Firefox):
   ```bash
   playwright install firefox
   ```
4. При работе с ZIP/ботом используйте переменные окружения (см. разделы ниже) и убедитесь, что `curl`, `exiftool` и `sqlite3` доступны в `PATH` при необходимости.

## Обзор репозитория

| Скрипт/модуль | Основное назначение |
| --- | --- |
| `profile_link_scanner.py` | Асинхронное сканирование профилей VSCO, запись ссылок в SQLite и извлечение EXIF метаданных. |
| `vsco_utils.py` | Общие функции нормализации URL, извлечения ссылок из HTML и взаимодействия с Playwright. |
| `exif_fetcher.py` | Фоновая выгрузка изображений с помощью `curl` и распаковка EXIF с использованием `exiftool`. |
| `vsco_downloader.py` | Полноценный загрузчик медиа (Playwright) с логированием и упаковкой в ZIP. |
| `zip_profile.py` | Лёгкий zip-бот без базы данных, скачивает профиль VSCO и отдаёт ZIP-файл. |
| `vsco_parser.py` | Разбор HTML/Excel выгрузок Visual Search, построение галерей и карт. |
| `vsco_rescan.py` | Массовое пересканирование уже сохранённых профилей с трекингом состояния. |
| `local_export_server.py` | Локальный HTTP-сервер для просмотра галерей/карт/CSV по базе. |
| `vsco_export.py` / `vsco_export_impl.py` | Хендлеры экспорта бота (CSV, галереи, карты) и инфраструктура для отправки файлов. |
| `vsco_bot.py` | Основная логика Telegram-бота: парсинг сообщений, хранение данных, построение экспорта и статистики. |
| `export_profile_html.py` | CLI-скрипт, который формирует единый HTML-файл для конкретного username с картой (если есть координаты) и галереей. |

### Экспорт профиля в HTML

Скрипт `export_profile_html.py` читает базу `vsco_links.db` (или путь из `BOT_DB_PATH`) и создаёт один HTML-файл с картой и галереей профиля.

Пример вызова:

```bash
python export_profile_html.py <username> \
  --db /path/to/vsco_links.db \
  --chat-id 123456789 \
  --out ~/Downloads/vsco_profile.html
```

* `<username>` — ник профиля VSCO (можно передавать с `@`).
* `--db` — путь до SQLite-базы с выгрузкой VSCO (по умолчанию `vsco_links.db` в текущей директории или путь из переменной окружения `BOT_DB_PATH`).
* `--chat-id` — необязательный фильтр для конкретного `chat_id`, если база содержит несколько профилей с одинаковым ником.
* `--out` — путь к итоговому HTML-файлу (если не указан, будет создан файл `export_<username>.html` в текущей папке).

После выполнения в консоли появится сообщение с путём к готовому HTML-файлу.
| `test_*.py` | Наборы автоматических тестов для критичных компонентов. |

Ниже — подробности по каждому модулю.

## Ограничения доступа и скачивания

Параметры задаются в `.env` и применяются после перезапуска бота.
Переменные окружения процесса имеют приоритет над `.env`.

| Параметр | Назначение |
| --- | --- |
| `BOT_REQUIRED_UNIQUE_LINKS=10` | Число новых уникальных ссылок на профили VSCO для доступа к боту. `0` отключает это условие. |
| `BOT_REQUIRED_CHANNEL_ID`, `BOT_REQUIRED_GROUP_ID` | Обязательное вступление в канал/группу: ID или `@username`. Пустой ID отключает соответствующую проверку. |
| `BOT_REQUIRED_CHANNEL_LINK`, `BOT_REQUIRED_GROUP_LINK` | Ссылки для вступления в обязательные сообщества. |
| `BOT_REQUIRED_CHANNEL_LABEL`, `BOT_REQUIRED_GROUP_LABEL` | Названия сообществ в сообщении о доступе. |
| `BOT_REQUIRED_ACCESS_CACHE_TTL=30` | Время кеширования проверки членства, в секундах. |
| `BOT_DAILY_PROFILE_LIMIT=20` | Минимум новых профилей за последние 24 часа для скачивания и экспорта. `0` отключает этот порог; остальные условия доступа сохраняются. Это не максимальное число скачиваний. |
| `BOT_ADMIN_IDS` | Telegram ID администраторов через запятую; они освобождены от этих ограничений. |

## Видимое окно браузера

В `.env` задайте `BOT_BROWSER_HEADLESS=0`, чтобы видеть окно Firefox во время
задач, использующих Playwright. Значение `1` (по умолчанию) включает headless —
работу без окна. После изменения перезапустите бота.

Настройка общая для скачивания, сканирования профилей и извлечения EXIF через
Playwright. Отдельно запущенные скрипты также читают `.env` рядом с исходниками,
независимо от текущего каталога; переменная окружения процесса имеет приоритет.
`BOT_INLINE_PLAYWRIGHT` по-прежнему отдельно управляет использованием браузерного
fallback в боте: переключатель headless сам по себе не включает Playwright
в задачах, выполняемых обычными HTTP-запросами.

Окно открывается на компьютере, где запущен бот. На Linux нужен графический
сеанс с настроенным `DISPLAY`; на удалённом сервере для просмотра потребуется
доступ к его рабочему столу, например через VNC/RDP. При нескольких параллельных
задачах может открыться несколько окон. На сервере без рабочего стола оставьте `1`.

Чтобы окно оставалось открытым после успеха или ошибки, включите режим просмотра:

```dotenv
BOT_BROWSER_HEADLESS=0
BOT_BROWSER_KEEP_OPEN=1
```

Браузер и его сессия остаются живыми до ручного закрытия всех его вкладок/окон.
Режим применяется к загрузчику, обоим сканерам и браузерному получению EXIF.
Ошибка записывается в журнал до ожидания закрытия окна.
**Задача остаётся в ожидании**: для `/dl` отправка файлов и переход к следующему
заданию произойдут после закрытия окна. Это режим отладки, а не отделённый от
задачи браузер. Отмена асинхронной задачи и остановка процесса не сохраняют окно.
Если браузер не смог запуститься или уже закрыт, сохранять нечего.

`BOT_BROWSER_KEEP_OPEN=0` (по умолчанию) возвращает автоматическое закрытие.
При `BOT_BROWSER_HEADLESS=1` сохранение окна игнорируется с предупреждением,
чтобы невидимый браузер не задерживал задачу. После изменения `.env` перезапустите бота.

## Рассылка при запуске

В `.env` рядом расположены переключатель и текст сообщения:

```dotenv
BOT_STARTUP_BROADCAST_ENABLED=0
BOT_STARTUP_BROADCAST_TEXT="Бот запущен и готов к работе!"
```

Поставьте `1`, чтобы включить рассылку, или `0`, чтобы выключить, и перезапустите
бота. Текст отправляется без HTML-разметки, максимум 4096 символов Telegram.
В двойных кавычках можно использовать `\n` для переноса строки.
Пустое или слишком длинное сообщение пропускается с предупреждением в журнале.

При каждом запуске процесса бот один раз рассылает сообщение всем известным
личным чатам, независимо от ограничений доступа. Список сохраняется в таблице
`bot_private_chats`: при обновлении он пополняется из личных чатов в таблицах
`links`, `items` и `comments`, затем — при личных сообщениях и нажатиях кнопок,
даже когда рассылка выключена. Пользователи, которые ранее только открыли бота
и не оставили записей в базе, попадут в список после следующего обращения.
Группы и каналы не включаются; одному чату отправляется одно сообщение.

Рассылка работает в фоне, не задерживая обработку команд. Повторное подключение
polling не запускает её заново. Между сообщениями выдерживается пауза,
при ограничении Telegram учитывается `retry_after` (до трёх попыток на чат).
Недоступные чаты пропускаются. При остановке бота незавершённая рассылка отменяется;
следующий запуск начинает новую рассылку.

## Уведомления о новых ссылках

Бот автоматически присылает дайджест свежих ссылок за последние 24 часа в выбранный чат.

1. Настройте чат для отправки через `BOT_LINKS_NOTIFY_CHAT_ID`. Поддерживаются `@username`,
   числовой идентификатор (`-100…`) или приватная ссылка `https://t.me/c/<id>` — значение
   будет автоматически преобразовано в нужный формат.【F:vsco_bot.py†L562-L636】
2. Чтобы ограничить количество ссылок на одной странице уведомления, задайте `BOT_LINKS_NOTIFY_LIMIT`
   (по умолчанию 20).【F:vsco_bot.py†L156-L205】【F:vsco_bot.py†L1866-L1998】
3. Чтобы присылать дайджест раз в сутки в определённое время, установите `BOT_LINKS_NOTIFY_AT`
   (формат `HH:MM`, часовой пояс бота — `LOCAL_TZ`, по умолчанию `UTC+03:00`). Без этой переменной
   сообщение отправляется сразу после появления новых ссылок.【F:vsco_bot.py†L512-L526】【F:vsco_bot.py†L542-L559】【F:vsco_bot.py†L1866-L1998】

Уведомление приходит одним сообщением; внутри доступна пагинация по страницам через inline-клавиатуру
без создания дополнительных сообщений.【F:vsco_bot.py†L1866-L1998】【F:vsco_bot.py†L5716-L5758】

## `profile_link_scanner.py`

### CLI сценарии

```bash
python profile_link_scanner.py --username vsco_user --chat-id 123456
python profile_link_scanner.py --profile-url "https://vsco.co/vsco_user/gallery" --chat-id 123456
```

Ключевые аргументы: `--db` (путь к SQLite), `--max-width` (апскейл `?w=`), `--target-count` и `--delay` для Playwright, `--verbose` для логов.【F:profile_link_scanner.py†L643-L715】

### Основные функции

| Функция | Назначение | Параметры / результат |
| --- | --- | --- |
| `resolve_profile_inputs(username, profile_url)` | Нормализует ввод CLI и возвращает пару `(username, profile_url)` в формате `https://vsco.co/<user>/gallery`. | Бросает `ValueError` при некорректных данных; удаляет `@` и пустые значения.【F:profile_link_scanner.py†L157-L175】 |
| `collect_profile_media(profile_url, *, max_width, delay, target_count, headers, include_details)` | Сначала пытается получить медиа через HTTP, затем при необходимости вызывает Playwright. Может возвращать `ProfileMediaCollection` с разбивкой по вкладкам. | Управление шириной апскейла, задержками и целевым количеством ссылок; автоматически закрывает сессии `aiohttp`.【F:profile_link_scanner.py†L218-L360】 |
| `store_profile_media(db_path, chat_id, username, profile_url, media_urls, *, source, added_by, profile_tabs, media_by_tab, meta_fetcher)` | Сохраняет ссылки в таблицы `items`/`links`, предотвращая дубли, и возвращает `ScanResult` с количеством добавленных записей и заданиями на метаданные. | Автоматически создаёт схему БД, обновляет `links.extra_json` вкладками и возвращает список `metadata_targets` для пост-обработки.【F:profile_link_scanner.py†L511-L640】 |
| `populate_media_metadata(db_path, items, *, meta_fetcher)` | По списку `(item_id, url)` извлекает EXIF и обновляет `items.meta_json`. | По умолчанию использует `extract_exif_from_url`, устойчиво к сбоям и логирует ошибки.【F:profile_link_scanner.py†L720-L760】 |

Дополнительные вспомогательные функции (`ensure_db_schema`, `_collect_with_playwright`, `connect_db`) отвечают за создание схемы, прокрутку Playwright и настройки SQLite.【F:profile_link_scanner.py†L79-L200】【F:profile_link_scanner.py†L178-L320】

## `vsco_utils.py`

### Обработка URL и медиа

| Функция | Описание |
| --- | --- |
| `normalize_vsco_profile_url(url)` | Приводит ссылки VSCO (включая `vs.co`) к виду `https://vsco.co/<username>/gallery`, поддерживает perception-hosts и короткие ссылки.【F:vsco_utils.py†L176-L208】 |
| `resolve_vsco_short_link(url, session, *, headers, max_hops, request_kwargs)` | Раскрывает редиректы `vs.co`, при необходимости возвращает perception-галерею; принимает `aiohttp.ClientSession`.【F:vsco_utils.py†L211-L260】 |
| `normalize_media_url`, `upscale_w_param`, `dedupe_keep_order` | Нормализация абсолютных ссылок, апскейл query-параметра `w` и удаление дублей, сохраняя порядок.【F:vsco_utils.py†L86-L137】 |

### Извлечение ссылок из HTML

| Функция | Описание |
| --- | --- |
| `extract_media_urls_from_html(html, *, max_width, root)` | Использует BeautifulSoup, чтобы собрать `img`, `picture` и `video` ресурсы с учётом `srcset`, фильтрует лого и апскейлит разрешение.【F:vsco_utils.py†L285-L335】 |
| `extract_profile_tab_links(html, *, root)` | Возвращает список вкладок профиля (`id`, `href`, `label`, `slug`, `active`) по `a[id^='profileTab-']`.【F:vsco_utils.py†L338-L382】 |
| `scan_profile_media(session, profile_url, *, max_width, limit, logger, request_kwargs)` | Делает HTTP-запрос, извлекает ссылки и возвращает `(urls, html)`; fallback парсит мета-теги OG/Twitter при пустом результате.【F:vsco_utils.py†L385-L425】 |
| `playwright_scan_profile(...)` | Асинхронно прокручивает галерею с нажатием `Load More`, управляет задержками, ограничением прокруток и fallback на HTTP. Требует установленный Playwright Firefox.【F:vsco_utils.py†L428-L671】 |

Дополнительно модуль содержит утилиты `generate_media_filename`, `extract_vsco_media_urls`, `vsco_short_slug`, `build_perception_gallery_url` и константы с доменами и регулярными выражениями.【F:vsco_utils.py†L140-L199】【F:vsco_utils.py†L263-L283】

## `exif_fetcher.py`

| Компонент | Назначение |
| --- | --- |
| `extract_exif_from_url(url, *, referer, timeout, headers)` | Скачивает файл через `curl`, вычисляет размер и, если доступен `exiftool`, возвращает дерево EXIF (`{"size_bytes": ..., "exiftool": {...}}`). Поддерживает переопределение заголовков и реферера.【F:exif_fetcher.py†L91-L147】 |
| `_build_curl_command`, `_call_exiftool` | Служебные функции подготовки аргументов `curl` и запуска `exiftool`, поднимают `ExifExtractionError`, если бинарники недоступны.【F:exif_fetcher.py†L27-L89】 |

## `vsco_downloader.py`

CLI-загрузчик медиа с Playwright, логированием и ZIP.

Основные аргументы: `--username/--profile-url`, `--out`, `--max`, `--concurrency`, `--delay`, `--timeout`, `--max-width`, `--no-zip`, `--zip-name`, `--skip-video-thumbs`, `--split-zip-size-mb`. Перед стартом проверяет наличие Playwright и BeautifulSoup.【F:vsco_downloader.py†L25-L117】

Ключевые функции:

- `setup_logger(username)` — создаёт логгер, файл логов и метку времени, возвращает `(logger, logfile_path, timestamp)`.【F:vsco_downloader.py†L41-L54】
- `pair_thumbnails_with_videos(urls, skip_thumbs)` — сопоставляет постер-изображения с видео и фильтрует миниатюры при необходимости.【F:vsco_downloader.py†L102-L160】
- `collect_image_urls(page, logger, delay, timeout, target_count, max_width, ...)` — (см. файл) обрабатывает прокрутку Playwright и сбор ссылок.
- Далее в файле описаны функции скачивания, валидации и упаковки (см. комментарии внутри).

## `zip_profile.py`

Лёгкий ZIP-бот без базы данных:

- `resolve_vsco_short_or_profile(u, stats)` — принимает `vs.co`/`vsco.co` и возвращает нормализованную ссылку, используя `normalize_vsco_profile_url` и `resolve_vsco_short_link`. Ведёт статистику запроса через `ZipStats`.【F:zip_profile.py†L95-L133】
- `_extract_site_id(html)` — ищет `site_id` на странице, чтобы обращаться к публичному API VSCO.【F:zip_profile.py†L134-L139】
- `fetch_vsco_api_image_urls(site_id, stats, max_urls)` — выгружает до `max_urls` ссылок с `https://vsco.co/api/2.0/medias`, учитывает разные поля и варианты изображения, детектирует ошибки. Возвращает список уникальных URL.【F:zip_profile.py†L141-L200】
- Остальная логика отвечает за сбор HTML, скачивание файлов и отправку ZIP через aiogram-router `zip_router` (см. файл).

## `vsco_parser.py`

Используется для обработки выгрузок Visual Search и последующего отображения.

| Функция | Назначение |
| --- | --- |
| `parse_html_file(path)` | Анализирует HTML: сначала ищет `users = [ {...} ]`, затем карточки `.image-card`, fallback — по классам `.username`/`.coordinates` или простым ссылкам. Возвращает список словарей с `username`, координатами, ссылками и превью.【F:vsco_parser.py†L67-L126】 |
| `read_excel(xlsx_path, sheet)` / `write_excel(rows, out_path, sheet)` | Конвертация Excel ↔️ список словарей, с автоматическим добавлением недостающих столбцов и приведением типов координат.【F:vsco_parser.py†L127-L157】 |
| `dedupe_rows(rows, mode)` | Удаляет дубликаты по изображениям, координатам или профилям, поддерживает режимы `safe`/`none`. Нормализует username/URL, округляет координаты.【F:vsco_parser.py†L179-L233】 |
| `build_gallery_html(items, title, subtitle)` | Генерирует статичную HTML-галерею с фильтрами и сеткой, внедряя данные в JSON.【F:vsco_parser.py†L245-L446】 |
| `build_map_html(...)` | Строит карту Leaflet с точками/кластерами (см. файл для подробностей).【F:vsco_parser.py†L447-L490】 |
| `main()` | CLI-обёртка: конвертирует HTML/CSV/Excel, строит карты/галереи (см. файл).【F:vsco_parser.py†L491-L525】 |

## `vsco_rescan.py`

Скрипт массового пересканирования существующей базы `items`/`links`.

- `connect_state_db(path)` — создаёт отдельную БД состояния с таблицей `processed` и включает WAL/таймауты.【F:vsco_rescan.py†L54-L75】
- `_load_profiles(conn, *, limit, chat_id, alphabetical)` — читает профили из `links`, возвращает список `(chat_id, username, profile_url)` с учётом фильтров и лимитов.【F:vsco_rescan.py†L103-L227】
- `_mark_profile_scanned(...)` и `_load_processed_urls(...)` управляют списком уже обработанных профилей, чтобы повторные запуски пропускали их.【F:vsco_rescan.py†L76-L102】
- `parse_args()` — CLI-аргументы (`--db`, `--state-db`, `--concurrency`, `--limit`, `--alphabetical`, `--max-width`, `--target-count`, `--delay`, `--chat-id`, `--verbose`).【F:vsco_rescan.py†L263-L366】
- `main()` — точка входа, планирует асинхронные задачи сканирования, обновляет состояние и печатает статистику.【F:vsco_rescan.py†L367-L428】

## `local_export_server.py`

Локальный HTTP-сервер для просмотра экспорта:

- `_render_index(refresh_interval)` — формирует главную страницу со статистикой по чатам и ссылками на галереи/карты/CSV. Использует `_list_chat_stats()` для подсчётов.【F:local_export_server.py†L58-L157】
- `_export_scope_from_query(params)` — определяет область (`chat`/`all`) и конкретный чат из query-параметров. Возвращает `(scope, chat_id, error)`.【F:local_export_server.py†L158-L178】
- `_generate_csv(scope, chat_id)` — собирает данные через `vsco_bot.fetch_gallery_users`, конвертирует в CSV-строку и возвращает bytes. Ошибки отображаются как HTTP 500.【F:local_export_server.py†L179-L337】
- `serve(host, port, refresh)` — запускает `http.server.ThreadingHTTPServer` с кастомным обработчиком, обновляющим HTML и CSV на лету.【F:local_export_server.py†L338-L351】
- `main(argv)` — парсит CLI (`--host`, `--port`, `--refresh`, `--db`) и вызывает `serve`. По умолчанию слушает `127.0.0.1:8765` и обновляет страницы каждые 60 секунд.【F:local_export_server.py†L352-L419】

## `vsco_export_impl.py` и `vsco_export.py`

`vsco_export.py` просто реэкспортирует `ExportDependencies` и `ExportManager` для обратной совместимости.【F:vsco_export.py†L1-L11】

### ExportManager

- При инициализации регистрирует обработчики `/export` и `callback_query` с префиксом `export:` и читает лимиты из `VSCO_EXPORT_ZIP_THRESHOLD` (по умолчанию 45 МБ) и 50 МБ лимит Telegram.【F:vsco_export_impl.py†L81-L112】
- `build_scope_keyboard(session)` — формирует inline-клавиатуру выбора области (текущий чат/вся база) и формата (CSV/галерея/карты).【F:vsco_export_impl.py†L93-L112】
- `open_menu(msg, user_id)` и `_cmd_export_handler` — проверяют права доступа, лимиты выгрузки и отображают меню экспорта в личных сообщениях.【F:vsco_export_impl.py†L114-L135】
- `on_export_click(cq)` — обрабатывает нажатия, переключает область/форматы и вызывает соответствующие методы отправки файлов.【F:vsco_export_impl.py†L136-L192】
- `_export_csv/_export_gallery/_export_map` — собирают данные через переданные зависимости (`fetch_gallery_users`, `build_rich_gallery`, `build_map_*`) и сохраняют файлы в директории сессии перед отправкой пользователю.【F:vsco_export_impl.py†L193-L290】
- `_send_path_document` и `_prepare_document_for_sending` — автоматически зипуют файлы при превышении порога и отправляют документ в Telegram, затем вызывают `mirror_export`, если он настроен (например, для отдельного архива/канала).【F:vsco_export_impl.py†L291-L392】

## `vsco_bot.py`

Главный модуль бота содержит большую коллекцию утилит. Ниже — наиболее используемые функции и области.

### Геоданные и метаданные

- `resolve_city_label(lat, lon)` — обратное геокодирование (через `reverse_geocoder` при наличии) или форматирование координат; кеширует результаты по тысячным долям градуса.【F:vsco_bot.py†L172-L200】
- `extract_coordinates_from_meta(meta)` — глубоко обходить словари/списки EXIF, чтобы найти координаты, учитывая `GPSLatitudeRef/GPSLongitudeRef` и альтернативные поля.【F:vsco_bot.py†L294-L357】
- `extract_camera_models_from_meta(meta)` — собирает уникальные метки моделей камеры из EXIF, нормализует пробелы и возвращает список строк.【F:vsco_bot.py†L362-L395】

### Управление источниками данных и статистикой

- `dataset_token_pairs(source, source_file)` — формирует пары `(значение, метка)` для отображения набора данных (файл/источник) в отчётах и картах.【F:vsco_bot.py†L398-L461】
- `fetch_gallery_users(scope, chat_id)` — агрегирует данные по пользователям: медиа по вкладкам, метаданные, города, камеры, вкладки профиля. Используется экспортом и локальным сервером.【F:vsco_bot.py†L2030-L2120】
- `fetch_items_for_map(scope, chat_id)` — готовит список отдельных медиа с координатами, комментариями, источниками и подсказками для карты.【F:vsco_bot.py†L2405-L2480】
- `build_rich_gallery(users, title, subtitle)` — генерирует HTML-галерею с панелью фильтров, стилями и встроенными данными; результат используется экспортом и локальным сервером.【F:vsco_bot.py†L2483-L2520】
- `build_map_users` / `build_map_images` — создают HTML-карты Leaflet для пользователей и изображений соответственно, с кластеризацией и всплывающими окнами (см. файл для деталей и кастомизаций).【F:vsco_bot.py†L4119-L4150】

### Работа с сообщениями и сохранением данных

Модуль содержит десятки вспомогательных функций для разбора сообщений Telegram, сохранения ссылок, построения клавиатур и админских инструментов. Ключевые точки интеграции с остальными скриптами:

- `persist_profile_media_urls` / `upsert_items_with_comments` — взаимодействуют с таблицами `items`, `links`, `comments` при добавлении ссылок из сканера или ручных сообщений (см. соответствующие участки файла).
- `get_session(chat_id)` — возвращает объект сеанса с настройками экспорта (используется `ExportManager`).【F:vsco_bot.py†L4557-L4563】
- `fetch_gallery_users`, `fetch_items_for_map`, `build_rich_gallery`, `build_map_users`, `build_map_images` — экспортные точки, описанные выше.

Из-за объёма файла рекомендуется искать нужную функцию через `rg "def <name>" vsco_bot.py`.

## `vsco_export.py`, `test_*` и прочее

- `vsco_export.py` — совместимый реэкспорт для модулей, которые импортируют `ExportManager` из старого пути.【F:vsco_export.py†L1-L11】
- Тесты (`test_profile_link_scanner.py`, `test_text_link_parsing.py`) демонстрируют пример использования функций и могут служить документацией по ожидаемому поведению.

## Проверка результатов сканирования

После работы скриптов ссылки находятся в таблице `items` базы `vsco_links.db`. Примеры команд:

```bash
sqlite3 vsco_links.db "SELECT image_url FROM items ORDER BY id DESC LIMIT 10;"
sqlite3 vsco_links.db "SELECT created_at, image_url FROM items WHERE username='vsco_user' ORDER BY created_at;"
sqlite3 -header -csv vsco_links.db "SELECT username, image_url, created_at FROM items" > links.csv
sqlite3 vsco_links.db "DELETE FROM items WHERE username='vsco_user';"
```

Для полного пересканирования используйте `vsco_rescan.py`, предварительно удалив файл состояния `vsco_rescan_state.db` (или указав альтернативный через `--state-db`).【F:profile_link_scanner.py†L623-L640】【F:vsco_rescan.py†L263-L428】

## Локальный просмотр и экспорт

- Запустите сервер экспорта:
  ```bash
  python local_export_server.py --port 8765 --refresh 60
  ```
  Откройте `http://127.0.0.1:8765/`, чтобы получить ссылки на галерею, карты и CSV по всей базе и по отдельным чатам.【F:local_export_server.py†L338-L419】
- Для Telegram-бота используйте команду `/export` и inline-меню из `ExportManager`, чтобы получить те же файлы напрямую в чат. Настройте зависимости `ExportDependencies`, передав функции из `vsco_bot.py`.【F:vsco_export_impl.py†L81-L392】

## Дополнительные советы

- Playwright по умолчанию запускает Firefox без окна (`BOT_BROWSER_HEADLESS=1`); для видимого окна задайте `0`. Если скролл не нужен, ограничивайте `--target-count`, чтобы ускорить сбор.
- Для получения метаданных запустите `populate_media_metadata` в фоне или после основного сканирования, чтобы избежать блокировок SQLite.【F:profile_link_scanner.py†L720-L760】
- Бот автоматически повторяет извлечение EXIF через Playwright при ответах CDN 403, предварительно открывая страницу профиля, чтобы использовать актуальные cookies и заголовки.
- Большинство скриптов поддерживают параметр `--verbose`, включающий подробные логи Playwright/HTTP.

Теперь у вас есть единая точка входа для понимания всех модулей и их функций: используйте таблицы и ссылки на код, чтобы быстро найти нужную реализацию и интегрировать её в свои сценарии.
