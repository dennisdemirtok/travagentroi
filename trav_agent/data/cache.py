"""Enkel fil-cache för API-svar. Sparar JSON på disk."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Optional

from ..config import CACHE_DIR

logger = logging.getLogger(__name__)


class FileCache:
    """Fil-baserad cache för ATG API-svar."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key_to_path(self, key: str) -> Path:
        """Konvertera en URL/nyckel till en filsökväg."""
        h = hashlib.md5(key.encode()).hexdigest()
        # Skapa en läsbar del av filnamnet
        readable = key.split("/")[-1][:50].replace("?", "_").replace("&", "_")
        return self.cache_dir / f"{readable}_{h}.json"

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """Hämta cachat värde, eller None om det inte finns."""
        path = self._key_to_path(key)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Cache-läsfel för {key}: {e}")
        return None

    def set(self, key: str, value: dict[str, Any]) -> None:
        """Spara ett värde i cachen."""
        path = self._key_to_path(key)
        try:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Cache-skrivfel för {key}: {e}")

    def clear(self) -> int:
        """Rensa hela cachen. Returnerar antal borttagna filer."""
        count = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink()
            count += 1
        return count

    def size(self) -> int:
        """Antal cachade poster."""
        return len(list(self.cache_dir.glob("*.json")))
