"""A fake YouTube (the RSS feed and the Shorts page) and a fake Discord, as httpx transports."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx

DATA = Path(__file__).parent / "data"
FEED_IDS = ["YWVekO4yGJg", "jZSPxfsgpvs"]
PAGE_IDS = ["jZSPxfsgpvs", "yR_-8G1B3R4", "7e_fPLc8cLM"]


def feed_entry(video_id: str, title: str, published: datetime) -> str:
    return f"""<entry>
  <id>yt:video:{video_id}</id>
  <yt:videoId>{video_id}</yt:videoId>
  <title>{title}</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/{video_id}"/>
  <published>{published.isoformat()}</published>
 </entry>"""


class FakeYouTube:
    def __init__(self) -> None:
        self.feed = (DATA / "shorts_feed.xml").read_text()
        self.page = (DATA / "shorts_page.html").read_text()
        self.feed_status = 200
        self.page_status = 200
        self.requests: list[httpx.Request] = []

    def add_to_feed(self, video_id: str, title: str, published: datetime) -> None:
        first = self.feed.index("<entry>")
        entry = feed_entry(video_id, title, published)
        self.feed = self.feed[:first] + entry + "\n " + self.feed[first:]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/feeds/videos.xml":
            return httpx.Response(self.feed_status, text=self.feed)
        if request.url.path.endswith("/shorts"):
            return httpx.Response(self.page_status, text=self.page)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


class FakeDiscord:
    def __init__(self) -> None:
        self.posted: list[tuple[int, dict]] = []  # (channel id, body)
        self.status = 200
        self._next_id = 9000

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"code": 50001, "message": "Missing Access"})
        channel_id = int(request.url.path.split("/")[-2])
        self.posted.append((channel_id, json.loads(request.content)))
        self._next_id += 1
        return httpx.Response(200, json={"id": str(self._next_id)})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    @property
    def contents(self) -> list[str]:
        return [body["content"] for _, body in self.posted]
