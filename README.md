# VSCO Profile Link Scanner

Этот репозиторий содержит вспомогательные скрипты для работы с профилями VSCO. Ниже описаны шаги, которые помогут запустить сканирование профиля и воспользоваться сохранёнными ссылками.

## 1. Подготовка окружения

1. Установите зависимости проекта (предполагается Python 3.11+):
   ```bash
   pip install -r requirements.txt
   ```
   Если файла `requirements.txt` нет, установите необходимые пакеты вручную:
   ```bash
   pip install playwright aiohttp
   ```
2. Один раз установите браузеры Playwright (необходимы для `playwright_scan_profile`):
   ```bash
   playwright install
   ```

## 2. Сканирование профиля без скачивания медиа

Скрипт `profile_link_scanner.py` повторяет «поисковую» часть бота: он собирает ссылки на медиа и заносит их в SQLite-базу в том же формате, что и основной бот.

Пример запуска по username:
```bash
python profile_link_scanner.py --username vsco_user --chat-id 123456
```

Пример запуска по ссылке на профиль:
```bash
python profile_link_scanner.py --profile-url "https://vsco.co/vsco_user/gallery" --chat-id 123456
```

Параметры:
- `--chat-id` — идентификатор чата, который будет сохранён в базе (можно использовать `0`, если значение неважно).
- `--db` — путь к базе данных (по умолчанию создаётся `vsco_links.db` рядом со скриптом).
- `--max-width` — максимальная ширина для преобразования ссылок на изображения (по умолчанию 2048).
- `--target-count` — целевое количество медиа, после которого Playwright перестанет прокручивать страницу (0 = до конца галереи).
- `--delay` — пауза между прокрутками в секундах.
- `--verbose` — включить подробные логи Playwright/HTTP-запросов.

После завершения скрипт выведет, сколько уникальных записей было добавлено и сколько ссылок обнаружено.

## 3. Как посмотреть сохранённые ссылки

Ссылки сохраняются в таблицу `items` базы `vsco_links.db`. Чтобы быстро проверить содержимое, можно воспользоваться встроенной утилитой `sqlite3`:

```bash
sqlite3 vsco_links.db "SELECT image_url FROM items ORDER BY id DESC LIMIT 10;"
```

Если нужно получить все новые ссылки для конкретного профиля:
```bash
sqlite3 vsco_links.db "SELECT created_at, image_url FROM items WHERE username='vsco_user' ORDER BY created_at;"
```

Для экспорта ссылок в файл CSV можно воспользоваться любым инструментом, например `sqlite3`:
```bash
sqlite3 -header -csv vsco_links.db "SELECT username, image_url, created_at FROM items" > links.csv
```

## 4. Очистка записей (опционально)

Чтобы удалить все ссылки конкретного профиля и начать сканирование заново:
```bash
sqlite3 vsco_links.db "DELETE FROM items WHERE username='vsco_user';"
```

Аналогично можно удалить сохранённую ссылку на профиль из таблицы `links`:
```bash
sqlite3 vsco_links.db "DELETE FROM links WHERE username='vsco_user';"
```

## 5. Массовое пересканирование существующей базы

Если у вас уже есть база `links`/`items` и нужно обновить все профили, воспользуйтесь скриптом `vsco_rescan.py`. Он читает таблицу `links`, повторно проходит профили и добавляет недостающие медиа, одновременно запускает несколько задач для ускорения процесса. Отдельная база состояния (`vsco_rescan_state.db` по умолчанию) хранит историю уже обработанных профилей, поэтому каждый новый запуск автоматически пропускает их и сканирует только свежие.

Пример запуска:
```bash
python vsco_rescan.py --db vsco_links.db --concurrency 5 --limit 100
```

Полезные параметры:
- `--chat-id` — ограничить пересканирование профилями конкретного чата.
- `--limit` — обработать только указанное количество профилей (например, для теста).
- `--concurrency` — число параллельных корутин сканирования; увеличьте его, если у вас достаточно ресурсов и пропускной способности.
- `--max-width`, `--target-count`, `--delay` — те же параметры, что и у одиночного сканера.
- `--state-db` — путь к отдельной базе состояния; можно указать другой файл, если хотите независимый счётчик или нужно перезапустить сканирование с нуля.
- `--alphabetical` — включить обход профилей в алфавитном порядке по `username`.
- `--verbose` — включить подробные логи, чтобы видеть прогресс.

Скрипт выводит итоговую статистику: сколько профилей было обработано, сколько новых ссылок добавлено и сколько записей пропущено благодаря базе состояния. Повторный запуск безопасен — `store_profile_media` по-прежнему предотвращает дубли по `username`, `image_url` и `profile_url`. Чтобы полностью пересканировать профили заново, удалите или переименуйте файл состояния `vsco_rescan_state.db` (или тот, что передан в `--state-db`).

## 6. Автоматизация и интеграция

Ниже приведён практический пример того, как использовать функции сканера прямо из кода бота.

### 6.1 Импорт и повторное использование базы

- Подключите модуль в коде бота:
  ```python
  from pathlib import Path

  from profile_link_scanner import collect_profile_media, store_profile_media
  ```
- Передайте путь к той же БД, с которой работает бот. Если бот уже знает путь (например, `settings.DB_PATH`), передайте его в `Path(settings.DB_PATH)`. При необходимости можно создать отдельную базу: `Path("/path/to/links.db")`.

### 6.2 Асинхронное получение ссылок

Функция `collect_profile_media` является корутиной и выполняет сетевые запросы, поэтому её нужно вызывать с `await` внутри существующего event loop бота (например, обработчика aiogram):

```python
async def handle_vsco_scan(message: Message, profile_url: str) -> None:
    media_urls = await collect_profile_media(
        profile_url,
        max_width=2048,
        delay=0.4,
        target_count=0,
    )
    result = store_profile_media(
        Path("vsco_links.db"),
        chat_id=message.chat.id,
        username=message.from_user.username or "",
        profile_url=profile_url,
        media_urls=media_urls,
        source="bot",
        added_by=str(message.from_user.id),
    )
    await message.answer(
        f"Добавлено ссылок: {result.added_items}, всего найдено: {len(result.media_urls)}"
    )
```

Если обработчик должен запускать сканирование «в фоне», можно использовать `asyncio.create_task`:

```python
async def start_background_scan(profile_url: str, chat_id: int, username: str) -> None:
    async def _scan() -> None:
        media_urls = await collect_profile_media(profile_url)
        store_profile_media(Path("vsco_links.db"), chat_id, username, profile_url, media_urls)

    asyncio.create_task(_scan())
```

### 6.3 Работа с результатом

- `store_profile_media` возвращает объект `ScanResult`. Он содержит поля `media_urls`, `added_items` и `link_added`, поэтому результат можно сохранить или отправить в лог.
- `collect_profile_media` можно переиспользовать, чтобы получить ссылки без немедленной записи в базу (например, для предпросмотра или фильтрации перед сохранением).

Эти шаги позволяют переиспользовать готовую логику сканера внутри бота, не блокируя основной поток и не создавая дублирующих таблиц в базе данных.

## 7. Извлечение EXIF-метаданных по прямой ссылке

Модуль `vsco_exif.py` можно запускать из командной строки, чтобы быстро проверить EXIF-данные по ссылке на оригинал снимка.

```bash
python vsco_exif.py "https://img.vsco.co/.../vsco_image.jpg"
```

Альтернативно можно вызвать модуль через `-m` без суффикса `.py`:

```bash
python -m vsco_exif "https://img.vsco.co/.../vsco_image.jpg"
```

> 💡 Если при запуске появляется сообщение `ModuleNotFoundError: __path__ attribute not found on 'vsco_exif' while trying to find 'vsco_exif.py'`, значит команда была выполнена как `python -m vsco_exif.py`. Нужно убрать `.py` из имени модуля или запускать файл напрямую, как показано выше.

По умолчанию результат выводится в формате JSON в одну строку. Чтобы получить отформатированный вывод и задать тайм-аут загрузки, используйте дополнительные параметры:

```bash
python vsco_exif.py "https://img.vsco.co/.../vsco_image.jpg" --timeout 5 --pretty
```

В ответе будут ключи EXIF (например, `Make`, `Model`, `DateTimeOriginal`). Если у изображения нет метаданных, скрипт вернёт пустой JSON `{}`. Ошибки загрузки выводятся в стандартный поток ошибок и сопровождаются логами в консоли.

Чтобы сохранить результат в файл и включить расширенное логирование, добавьте опции `--output`, `--log-level` и `--log-file`:

```bash
python vsco_exif.py "https://img.vsco.co/.../vsco_image.jpg" \
  --output exif.json \
  --log-level DEBUG \
  --log-file exif.log
```

Команда создаст JSON-файл с метаданными и лог-файл с подробным ходом выполнения (старт запроса, количество тегов, сохранение файла, возможные ошибки).
