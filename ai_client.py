"""GigaChat API client: OAuth token, file storage, speech transcription and streamed answers.

Settings come from ``.env`` next to this file (or the environment):

    GIGACHAT_AUTH_KEY      authorization key from the GigaChat project (required)
    GIGACHAT_SCOPE         GIGACHAT_API_PERS (default) / GIGACHAT_API_B2B / GIGACHAT_API_CORP
    GIGACHAT_MODEL         model for answers (default GigaChat-3-Ultra)
    GIGACHAT_VISION_MODEL  model that reads screenshots (default GigaChat-2-Max)
    GIGACHAT_STT_MODEL     model for speech transcription (default GigaChat-2-Pro)
    GIGACHAT_BASE_URL      default https://api.giga.chat/v1
    GIGACHAT_MAX_CONCURRENCY  parallel requests allowed by the plan (Freemium: 1)

GigaChat's servers use the Russian Trusted Root CA, which macOS does not trust
by default; the certificate is pinned from ``certs/`` for these requests only.
Only audio-capable models accept audio attachments: GigaChat-2-Pro / 2-Max /
3-Pro / 3-Ultra (not GigaChat-2 or GigaChat-3-Lightning).

HTTP/2 is required for streaming: over HTTP/1.1 the gateway holds the whole
answer back until it is complete.
"""
import io
import json
import os
import ssl
import sys
import threading
import time
import uuid
from contextlib import contextmanager
import wave
from pathlib import Path

import httpx
import numpy as np

HERE = Path(__file__).resolve().parent
CA_BUNDLE = HERE / "certs" / "russian_trusted_root_ca.pem"
OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
RETRIES = 4
RETRY_STATUSES = (429, 500, 502, 503, 504)

TRANSCRIBE_PROMPT = (
    "Дословно транскрибируй речь из аудио. Речь в основном на русском, но встречаются "
    "английские IT-термины (embedding, LLM, encoder, fine-tuning, RAG, backend, deploy и т.п.) — "
    "пиши их латиницей, как в оригинале. Ничего не добавляй, не исправляй и не комментируй. "
    "Выведи только распознанный текст. Если речи нет (тишина, шум, музыка без слов) — выведи пустую строку."
)

SCREEN_PROMPT = (
    "Перепиши дословно весь текст со скриншота, который относится к содержимому (код, формулы, задания, "
    "вопросы, текст слайда, сообщения в чате), сохраняя все символы, операторы и отступы. Код оформи "
    "блоком ```. Элементы интерфейса программ (меню, кнопки, панели) не переписывай. Ничего не объясняй."
)

SYSTEM_PROMPT = (
    "Ты — незаметный ассистент пользователя во время звонка или собеседования. "
    "Тебе дают расшифровку разговора: «Я» — это пользователь (его микрофон), «Собеседник» — "
    "всё, что звучит из динамиков компьютера (другие участники звонка, видео и т.п.). "
    "Расшифровка автоматическая и содержит ошибки распознавания, особенно в английских терминах, "
    "записанных кириллицей или похожими словами (например «рек»/«раг» — RAG, «эл эл эм» — LLM, "
    "«эмбеддинг» — embedding, «энкодер» — encoder). Восстанавливай смысл по контексту.\n"
    "Когда просят ответить по диалогу — найди последний вопрос или задачу собеседника и дай "
    "ответ, который пользователь может сразу произнести: сначала суть в 1–2 предложениях, затем "
    "ключевые детали коротким списком. Если для ответа нужно что-то вычислить или проследить "
    "выполнение кода — сначала кратко посчитай по шагам, потом итог: не угадывай результат. "
    "Формулы пиши в LaTeX: $...$ в строке, $$...$$ отдельной строкой. "
    "Отвечай на языке разговора (обычно русский), "
    "IT-термины пиши латиницей. Без вступлений и воды."
)


def load_env(path: Path = HERE / ".env"):
    """Minimal .env reader: KEY=VALUE lines; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # -sig: Windows Notepad may add a BOM
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class GigaChatError(RuntimeError):
    pass


class AnswerInterrupted(GigaChatError):
    """The connection dropped after part of the answer had already arrived."""


CLEANUP, BACKGROUND, URGENT = 0, 1, 2  # request priorities


class _Gate:
    """Limits concurrent requests; a free slot goes to the most urgent waiter.

    Freemium GigaChat allows a single request at a time ("1 flow") and answers
    HTTP 429 to the rest, so requests queue here instead, and an answer the user
    is waiting for skips ahead of background transcription and file cleanup.
    """

    def __init__(self, limit: int):
        self.limit, self.active = limit, 0
        self.waiting = [0, 0, 0]  # per priority
        self.cond = threading.Condition()

    @contextmanager
    def slot(self, priority: int, timeout: float | None = None):
        """Raises TimeoutError if no slot frees up within ``timeout`` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            self.waiting[priority] += 1
            try:
                while self.active >= self.limit or any(self.waiting[priority + 1:]):
                    left = None if deadline is None else deadline - time.monotonic()
                    if left is not None and left <= 0:
                        raise TimeoutError("no free request slot")
                    self.cond.wait(left)
            finally:
                self.waiting[priority] -= 1
            self.active += 1
        try:
            yield
        finally:
            with self.cond:
                self.active -= 1
                self.cond.notify_all()


class GigaChatClient:
    def __init__(self):
        load_env()
        self.auth_key = os.environ.get("GIGACHAT_AUTH_KEY", "")
        if not self.auth_key:
            raise GigaChatError("GIGACHAT_AUTH_KEY is not set (put it in .env)")
        self.scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
        self.model = os.environ.get("GIGACHAT_MODEL", "GigaChat-3-Ultra")
        self.vision_model = os.environ.get("GIGACHAT_VISION_MODEL", "GigaChat-2-Max")
        self.stt_model = os.environ.get("GIGACHAT_STT_MODEL", "GigaChat-2-Pro")
        self.base_url = os.environ.get("GIGACHAT_BASE_URL", "https://api.giga.chat/v1").rstrip("/")
        # The settings panel can override it; this is what "restore default" goes back to.
        self.default_system_prompt = os.environ.get("GIGACHAT_SYSTEM_PROMPT", SYSTEM_PROMPT)
        self.system_prompt = self.default_system_prompt
        verify = ssl.create_default_context(cafile=str(CA_BUNDLE)) if CA_BUNDLE.exists() else True
        # One shared client (thread-safe); HTTP/2 multiplexes parallel requests over one connection.
        # A request that hangs holds the single allowed slot, so give up on it after 30 s without
        # data (a transcription takes ~1 s; a stream sends a chunk at least every few seconds).
        self.http = httpx.Client(http2=True, verify=verify, timeout=httpx.Timeout(30, connect=10, write=60))
        self._token, self._expires = "", 0.0
        self._token_lock = threading.Lock()
        self._gate = _Gate(int(os.environ.get("GIGACHAT_MAX_CONCURRENCY", "1")))
        self.closing = False  # set on quit: a streaming answer stops early and frees the request slot
        self._priority = threading.local()

    @contextmanager
    def urgent(self):
        """Requests made by this thread inside the block go ahead of background work."""
        self._priority.value = URGENT
        try:
            yield
        finally:
            self._priority.value = BACKGROUND

    def _slot(self, priority: int | None = None):
        return self._gate.slot(priority if priority is not None else getattr(self._priority, "value", BACKGROUND))

    # ---------- plumbing ----------
    def token(self) -> str:
        with self._token_lock:
            if time.time() > self._expires - 60:
                r = self.http.post(OAUTH_URL, data={"scope": self.scope}, headers={
                    "Authorization": f"Basic {self.auth_key}", "RqUID": str(uuid.uuid4()),
                    "Accept": "application/json"})
                self._check(r)
                body = r.json()
                self._token, self._expires = body["access_token"], body["expires_at"] / 1000
            return self._token

    @staticmethod
    def _check(r: httpx.Response):
        if r.status_code >= 400:
            r.read()
            try:
                msg = r.json().get("message", r.text)
            except ValueError:
                msg = r.text
            raise GigaChatError(f"GigaChat HTTP {r.status_code}: {msg}")

    def _post(self, path: str, stream: bool = False, **kwargs) -> httpx.Response:
        """POST with retries on rate limiting (429), transient server errors and dropped connections.

        With ``stream=True`` the caller must close the response.
        """
        for attempt in range(RETRIES + 1):
            request = self.http.build_request("POST", f"{self.base_url}{path}", **kwargs, headers={
                "Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
            try:
                r = self.http.send(request, stream=stream)
            except httpx.TransportError as exc:  # e.g. the gateway resetting an HTTP/2 stream
                if attempt == RETRIES:
                    raise GigaChatError(f"connection to GigaChat failed: {exc!r}") from exc
                print(f"GigaChat connection error on {path} ({exc!r}), retrying", file=sys.stderr)
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code not in RETRY_STATUSES or attempt == RETRIES:
                break
            r.close()
            try:
                delay = float(r.headers.get("Retry-After", ""))
            except ValueError:
                delay = 0.5 * 2 ** attempt
            delay = min(delay, 8)
            print(f"GigaChat HTTP {r.status_code} on {path}, retry in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
        self._check(r)
        return r

    def warm_up(self):
        """Fetch a token and open the connection ahead of the first real request."""
        self.http.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.token()}"})

    def close(self):
        self.http.close()

    # ---------- files ----------
    def upload(self, data: bytes, filename: str, mime: str) -> str:
        with self._slot():
            r = self._post("/files", files={"file": (filename, data, mime)}, data={"purpose": "general"})
        return r.json()["id"]

    def delete_file(self, file_id: str, wait: float | None = None):
        """Best effort: storage cleanup must never break or hold up the app.

        ``wait``: give up if no request slot frees up within this many seconds.
        """
        try:
            with self._gate.slot(CLEANUP, wait):
                self.http.post(f"{self.base_url}/files/{file_id}/delete", timeout=10,
                               headers={"Authorization": f"Bearer {self.token()}"})
        except (httpx.HTTPError, GigaChatError, TimeoutError):
            pass

    # ---------- generation ----------
    def complete(self, messages: list[dict], model: str | None = None, **params) -> str:
        with self._slot():
            r = self._post("/chat/completions", json={"model": model or self.model, "messages": messages, **params})
        return r.json()["choices"][0]["message"]["content"]

    def stream(self, messages: list[dict], model: str | None = None, **params):
        """Yields text deltas as soon as GigaChat sends them (server-sent events).

        A connection drop before the first delta is retried quietly; after it,
        AnswerInterrupted is raised so the caller can say so and start over.
        """
        with self._slot():
            for attempt in range(RETRIES + 1):
                started = False
                try:
                    for piece in self._stream(messages, model, **params):
                        started = True
                        yield piece
                    return
                except httpx.TransportError as exc:
                    if started:
                        raise AnswerInterrupted(f"connection dropped mid-answer: {exc!r}") from exc
                    if attempt == RETRIES:
                        raise GigaChatError(f"connection to GigaChat failed: {exc!r}") from exc
                    print(f"GigaChat stream error before the first word ({exc!r}), retrying", file=sys.stderr)
                    time.sleep(0.5 * 2 ** attempt)

    def _stream(self, messages, model, **params):
        r = self._post("/chat/completions", stream=True,
                       json={"model": model or self.model, "messages": messages, "stream": True, **params})
        try:
            for line in r.iter_lines():
                if self.closing:
                    break
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                for choice in event.get("choices", []):
                    if text := choice.get("delta", {}).get("content"):
                        yield text
        finally:
            r.close()

    def transcribe_file(self, file_id: str) -> str:
        text = self.complete([{"role": "user", "content": TRANSCRIBE_PROMPT, "attachments": [file_id]}],
                             model=self.stt_model, temperature=0.01)
        return " ".join(text.strip().strip('"«»').split())

    # ---------- chat window interface ----------
    def read_screen(self, image_id: str) -> str:
        """Text of a screenshot (code, task, slide). A separate step on purpose: asked to read
        and answer in one go, GigaChat misreads code far more often (e.g. ``**`` as ``*``)."""
        return self.complete([{"role": "user", "content": SCREEN_PROMPT, "attachments": [image_id]}],
                             model=self.vision_model, temperature=0.01).strip()

    def ask_stream(self, prompt: str, screen_text: str | None = None, transcript: str = "",
                   from_dialog: bool = False):
        """Stream an answer.

        ``transcript`` is the dialog so far. ``from_dialog`` means the user asked to
        answer the conversation (⌘↵ / ⌘⇧↵) rather than a typed question; ``prompt`` is
        then an optional extra instruction. ``screen_text`` is what read_screen() found.
        """
        if from_dialog and screen_text is not None:
            request = (
                f"Текст с экрана пользователя (распознан со скриншота):\n{screen_text or '(пусто)'}\n\n"
                "Скорее всего, собеседник показывает на экране задачу или вопрос.\n"
                "1. Первой строкой напиши «На экране: …» — коротко, что именно там спрашивают. Если вопроса "
                "на экране нет, так и напиши.\n"
                "2. Если собеседник просит ответить на вопрос с экрана — отвечай именно на него, а не на "
                "вопросы, которые звучали в разговоре раньше.\n"
                "3. Если нужно что-то вычислить или выполнить код — сначала посчитай по шагам (кратко, "
                "шаг на строку, ничего не пропуская), потом дай итог.")
        elif from_dialog:
            request = "Ответь на последний вопрос собеседника."
        else:
            request = prompt or "Что мне ответить?"
        if from_dialog and prompt:
            request += f"\nДополнительно: {prompt}"
        if transcript:
            request = f"Расшифровка разговора:\n{transcript}\n\n{request}"
        messages = [{"role": "system", "content": self.system_prompt}, {"role": "user", "content": request}]
        yield from self.stream(messages, temperature=0.1 if screen_text is not None else 0.3)
