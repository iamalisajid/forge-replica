"""Run directory: typed artifacts (hashed) and evidence blobs, outside every git repo (~/.forge/runs/<id>)."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

from forge.core.config import APPS_ROOT, DEFAULT_MODEL, RUNS_DIR
from forge.core.ledger import Ledger
from forge.models.run import RunConfig

M = TypeVar("M", bound=BaseModel)


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"not an http(s) URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def site_of(url_or_origin: str) -> str:
    """Approximate registrable domain: app.example.com and api.example.com share the site example.com.
    IPs and localhost are their own site."""
    host = urlparse(url_or_origin).hostname or url_or_origin
    if host == "localhost" or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def same_site(url: str, site: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == site or host.endswith("." + site)


def first_party(url: str, site: str) -> bool:
    """Same site, or a host carrying the site's brand label (example.b-cdn.net, flags.example.net)."""
    if same_site(url, site):
        return True
    brand = site.split(".")[0]
    host = urlparse(url).hostname or ""
    return bool(brand) and not brand.isdigit() and brand != "localhost" and brand in host.split(".")


STATIC_TYPES = ("script", "stylesheet", "font", "image", "media", "manifest", "texttrack")


WRITE_MARKERS = ('"commands":[{', '"commands": [{', "commands=%5B%7B", "commands=[{", "mutation ", "mutation{",
                 '"mutation"')


# Body keys that mark an incremental-sync read in common sync protocols; with no write marker, such a POST is a read.
SYNC_READ_KEYS = ("sync_token", "resource_types", "delta_token", "deltaToken", "sync_cursor", "since_token", "seq_no")


def is_sync_read(text: str) -> bool:
    return any(k in text for k in SYNC_READ_KEYS)


def looks_like_write(method: str, body: str | None) -> bool:
    """Is this request a write? GET/HEAD/OPTIONS never are. A POST is a read only when it is recognisably one:
    an incremental-sync read (a SYNC_READ_KEYS key, no commands) or a GraphQL query (no mutation). Every other
    POST, and every PUT/PATCH/DELETE, is a write."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if method != "POST":
        return True
    text = (body or "").replace(" ", "")
    if any(m.replace(" ", "") in text for m in WRITE_MARKERS):
        return True
    if is_sync_read(text):
        return False
    if '"query"' in text and "mutation" not in text:
        return False
    return True


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RunStore:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        (self.dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.dir / "evidence").mkdir(parents=True, exist_ok=True)
        self._ledger: Ledger | None = None

    @classmethod
    def create(cls, url: str, *, mutations: str = "read-only", budget_usd: float = 15.0,
               model: str = DEFAULT_MODEL, pilot_only: bool = False, no_build: bool = False,
               heldout_dir: str | None = None, out_root: Path = APPS_ROOT,
               runs_dir: Path = RUNS_DIR, features: list[str] | None = None) -> tuple["RunStore", RunConfig]:
        run_id = new_run_id()
        store = cls(runs_dir / run_id)
        config = RunConfig(
            run_id=run_id, url=url, origin=origin_of(url), mutations=mutations, budget_usd=budget_usd,
            model=model, pilot_only=pilot_only, no_build=no_build, heldout_dir=heldout_dir,
            out_root=str(out_root), run_dir=str(store.dir), created_at=datetime.now(timezone.utc).isoformat(),
            features=features or ["crud"])
        store.write("run", config)
        return store, config

    @classmethod
    def open(cls, run_id: str, runs_dir: Path = RUNS_DIR) -> "RunStore":
        path = runs_dir / run_id
        if not (path / "artifacts" / "run.json").exists():
            raise FileNotFoundError(f"no run {run_id} in {runs_dir}")
        return cls(path)

    @property
    def config(self) -> RunConfig:
        return self.read("run", RunConfig)

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            budget = self.config.budget_usd if (self.dir / "artifacts" / "run.json").exists() else None
            self._ledger = Ledger(self.dir / "ledger.sqlite", budget)
        return self._ledger

    # ---- artifacts ----
    def artifact_path(self, name: str) -> Path:
        return self.dir / "artifacts" / f"{name}.json"

    def write(self, name: str, model: BaseModel) -> str:
        data = json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=False).encode()
        self.artifact_path(name).write_bytes(data)
        return sha256_bytes(data)

    def read(self, name: str, cls: type[M]) -> M:
        return cls.model_validate_json(self.artifact_path(name).read_bytes())

    def has(self, name: str) -> bool:
        return self.artifact_path(name).exists()

    def sha(self, name: str) -> str:
        return sha256_bytes(self.artifact_path(name).read_bytes())

    # ---- evidence ----
    def put_blob(self, data: bytes, ext: str) -> str:
        digest = sha256_bytes(data)
        path = self.dir / "evidence" / f"{digest}.{ext}"
        if not path.exists():
            path.write_bytes(data)
        return digest

    def blob_path(self, digest: str) -> Path:
        matches = list((self.dir / "evidence").glob(f"{digest}.*"))
        if not matches:
            raise FileNotFoundError(digest)
        return matches[0]
