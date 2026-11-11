"""Where interview recordings are kept: a private Supabase Storage bucket, or a local folder.

Both stores take paths like `interviews/{interview_id}/candidate.wav`. The Supabase service key
is a secret: it is sent only in the Authorization header and never reaches an error, a log or a
repr (ruling R19)."""

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

import httpx

# Supabase lists at most this many objects per request. An interview holds two files.
LIST_LIMIT = 100


class AudioStoreError(Exception):
    """A storage call failed. The message is the HTTP status and nothing else, so it can be
    logged without leaking the key, the project URL or the response body."""

    def __init__(self, status: int) -> None:
        super().__init__(f"audio storage returned HTTP {status}")
        self.status = status


class AudioStore(Protocol):
    async def put_wav(self, path: str, data: bytes) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    async def ensure_ready(self) -> None: ...


class LocalAudioStore:
    """Recordings in a folder on disk, for running without Supabase. File IO runs in a thread
    so writing a large WAV does not block the event loop that relays audio."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def _resolve(self, path: str) -> Path:
        target = (self._root / path).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("path escapes the recordings folder")
        return target

    async def put_wav(self, path: str, data: bytes) -> None:
        target = self._resolve(path)

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(write)

    async def delete_prefix(self, prefix: str) -> None:
        target = self._resolve(prefix)
        if target == self._root:
            raise ValueError("refusing to delete the whole recordings folder")
        await asyncio.to_thread(shutil.rmtree, target, True)

    async def ensure_ready(self) -> None:
        """Nothing to prepare: put_wav creates folders as it needs them."""


class SupabaseAudioStore:
    """Recordings in a private Supabase Storage bucket, over its REST API."""

    def __init__(self, url: str, service_key: str, bucket: str, http: httpx.AsyncClient) -> None:
        self._url = url.rstrip("/")
        self._key = service_key
        self._bucket = bucket
        self._http = http

    def __repr__(self) -> str:
        return f"SupabaseAudioStore(bucket={self._bucket!r})"

    async def _send(
        self, method: str, path: str, *, ok: tuple[int, ...] = (), **kwargs
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._key}", **kwargs.pop("headers", {})}
        response = await self._http.request(
            method, f"{self._url}/storage/v1/{path}", headers=headers, **kwargs
        )
        if not response.is_success and response.status_code not in ok:
            raise AudioStoreError(response.status_code)
        return response

    async def put_wav(self, path: str, data: bytes) -> None:
        await self._send(
            "POST",
            f"object/{self._bucket}/{path}",
            content=data,
            headers={"Content-Type": "audio/wav", "x-upsert": "true"},
        )

    async def delete_prefix(self, prefix: str) -> None:
        listing = await self._send(
            "POST",
            f"object/list/{self._bucket}",
            json={"prefix": prefix, "limit": LIST_LIMIT},
        )
        folder = prefix.rstrip("/")
        names = [f"{folder}/{item['name']}" for item in listing.json()]
        if not names:
            return
        await self._send("DELETE", f"object/{self._bucket}", json={"prefixes": names})

    async def ensure_ready(self) -> None:
        """Create the private bucket. It already existing (409) is fine."""
        await self._send(
            "POST",
            "bucket",
            ok=(409,),
            json={"id": self._bucket, "name": self._bucket, "public": False},
        )
