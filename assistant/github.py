"""Fetch a public GitHub repository as a ZIP, so it can take the same path as an upload.

Only https://github.com/<owner>/<repo>[/tree/<branch>] links are accepted. The
server asks GitHub's API for the archive and follows exactly one redirect, and
only to codeload.github.com, so a link can never make the server fetch an
arbitrary or internal address. The download is size-limited while streaming.
GitHub wraps everything in a top-level `<owner>-<repo>-<sha>/` folder; that
folder is removed after the archive passes the safety checks.
"""

import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import requests
from django.conf import settings

API = "https://api.github.com"
DOWNLOAD_HOST = "codeload.github.com"
TIMEOUT = 30

NAME = r"[A-Za-z0-9_.\-]{1,100}"
URL = re.compile(rf"^https?://(?:www\.)?github\.com/(?P<owner>{NAME})/(?P<repo>{NAME}?)(?:\.git)?"
                 rf"(?:/tree/(?P<ref>[A-Za-z0-9_.\-/]{{1,200}}))?/?$")


class GitHubError(Exception):
    def __init__(self, message_fa, next_fa=""):
        super().__init__(message_fa)
        self.message_fa = message_fa
        self.next_fa = next_fa


def parse_url(text):
    """Return (owner, repo, ref or None), or None if the text is not a supported GitHub link."""
    match = URL.match((text or "").strip())
    if not match:
        return None
    owner, repo, ref = match.group("owner"), match.group("repo"), match.group("ref")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner in (".", "..") or repo in (".", "..", "") or (ref and (".." in ref.split("/") or ref.startswith("/"))):
        return None
    return owner, repo, (ref.rstrip("/") if ref else None)


def download(owner, repo, ref, dest, max_bytes):
    """Download the repository archive to `dest`. Raises GitHubError with a Persian message."""
    url = f"{API}/repos/{owner}/{repo}/zipball" + (f"/{ref}" if ref else "")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "liara-assistant"}
    if settings.GITHUB_TOKEN:
        # Optional server token: raises GitHub's limit of 60 anonymous downloads per hour per IP.
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    try:
        first = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        raise GitHubError("گیت‌هاب الان پاسخ نمی‌دهد.", "چند دقیقه بعد دوباره امتحان کن یا فایل zip بفرست.") from None
    if first.status_code == 404:
        raise GitHubError("این مخزن یا شاخه پیدا نشد، یا خصوصی است.",
                          "فعلاً فقط مخزن‌های عمومی پشتیبانی می‌شوند؛ برای مخزن خصوصی فایل zip بفرست.")
    if first.status_code in (403, 429):
        raise GitHubError("سقف درخواست به گیت‌هاب برای این سرور پر شده.", "کمی بعد دوباره امتحان کن یا فایل zip بفرست.")
    location = first.headers.get("Location", "")
    if first.status_code not in (301, 302, 307, 308) or urlsplit(location).hostname != DOWNLOAD_HOST \
            or urlsplit(location).scheme != "https":
        raise GitHubError("پاسخ گیت‌هاب قابل استفاده نبود.", "فایل zip پروژه را مستقیم بفرست.")
    try:
        with requests.get(location, headers={"User-Agent": "liara-assistant"}, timeout=TIMEOUT,
                          stream=True, allow_redirects=False) as resp:
            if resp.status_code != 200:
                raise GitHubError("دانلود مخزن از گیت‌هاب ناموفق بود.", "دوباره امتحان کن یا فایل zip بفرست.")
            size = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise GitHubError(f"این مخزن بزرگ‌تر از {max_bytes // (1024 * 1024)} مگابایت است.",
                                          "فایل‌های بزرگ را از مخزن حذف کن یا یک zip کوچک‌تر بفرست.")
                    fh.write(chunk)
    except requests.RequestException:
        raise GitHubError("دانلود مخزن از گیت‌هاب قطع شد.", "دوباره امتحان کن یا فایل zip بفرست.") from None


def strip_top_folder(src, dest):
    """Rewrite GitHub's archive without its single top-level folder.

    Call only after inspect_zip() accepted `src`: entries are already known to be
    safe and within size limits."""
    with zipfile.ZipFile(src) as zin:
        names = [i.filename for i in zin.infolist()]
        tops = {PurePosixPath(n).parts[0] for n in names if PurePosixPath(n).parts}
        if len(tops) != 1:
            return False
        prefix = tops.pop() + "/"
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.is_dir() or not info.filename.startswith(prefix) or info.filename == prefix:
                    continue
                zout.writestr(info.filename[len(prefix):], zin.read(info))
    return True
