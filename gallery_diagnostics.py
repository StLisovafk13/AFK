"""Report failed VSCO pagination without treating the first page as a full export."""

from urllib.parse import urlsplit

from bs4 import BeautifulSoup


class GalleryLoadError(RuntimeError):
    pass


class GalleryLoadMonitor:
    def __init__(self, logger):
        self.logger = logger
        self.failure = None

    def on_response(self, response):
        url = urlsplit(response.url)
        is_media_api = (
            url.hostname == "vsco.co" and url.path == "/api/3.0/medias/profile"
        ) or (
            url.hostname == "media-grpc-api.vsco.co" and url.path == "/media.Media/FetchImages"
        )
        if not is_media_api or response.status < 400:
            return
        # Never log cookies, authorization headers, query strings or cursors.
        self.failure = (
            f"VSCO media API: HTTP {response.status} at {url.hostname}{url.path}. "
            "Полная загрузка профиля не подтверждена."
        )
        ray = response.headers.get("cf-ray")
        if ray:
            self.failure += f" Cloudflare Ray ID: {ray}."
        self.logger.error(self.failure)

    def check(self, html):
        if self.failure:
            raise GalleryLoadError(self.failure)
        soup = BeautifulSoup(html, "html.parser")
        # Application scripts can contain error labels even on healthy pages.
        for tag in soup(["script", "style", "template"]):
            tag.decompose()
        text = " ".join(soup.stripped_strings).lower()
        if "sorry, you have been blocked" in text:
            raise GalleryLoadError("Cloudflare заблокировал страницу VSCO. Полная загрузка не подтверждена.")
        if "error loading content" in text:
            raise GalleryLoadError(
                "VSCO сообщает Error Loading Content. Получена только часть профиля; "
                "загрузка остановлена. Проверьте доступ к API медиа и CDN."
            )
