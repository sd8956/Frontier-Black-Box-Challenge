"""Explorer log: one JSON line per request to the original.

Exact fields: ``id``, ``seq``, ``x_req``, ``hipotesis``, ``request``,
``status``, ``response`` and ``conclusion`` (optional). The token and the
``Authorization`` header are never written.

The hypothesis is passed by the caller, before the request. This module does
not invent it.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "experimentos.jsonl"


def payload_of(message):
    """Splits ``"CONSULTA;AC-0001;14"`` into ``("CONSULTA;AC-0001", 14)``.

    Returns ``(None, None)`` if the last field is not an integer.
    """
    if not isinstance(message, str):
        return None, None
    parts = message.split(";")
    if len(parts) < 2:
        return None, None
    last = parts[-1]
    if not last.isdigit():
        return None, None
    return ";".join(parts[:-1]), int(last)


def is_control_accepted(response):
    """A control is accepted when the body is a JSON object whose ``error``
    is neither ``E201`` (malformed) nor ``E202`` (invalid)."""
    if not isinstance(response, dict):
        return False
    return response.get("error") not in ("E201", "E202")


class ExperimentLogBase:
    """Shared id and measurement-reading logic."""

    def __init__(self):
        self._max_id = 0
        self._ids = set()
        self._records = []

    @property
    def records(self):
        return list(self._records)

    def _reserve_id(self, explicit_id=None):
        if explicit_id is not None:
            if not isinstance(explicit_id, int):
                raise ValueError("the id must be an integer")
            if explicit_id in self._ids:
                raise ValueError("duplicate id: %r" % (explicit_id,))
            self._ids.add(explicit_id)
            if explicit_id > self._max_id:
                self._max_id = explicit_id
            return explicit_id
        self._max_id += 1
        while self._max_id in self._ids:
            self._max_id += 1
        self._ids.add(self._max_id)
        return self._max_id

    def _build(
        self, seq, x_req, hipotesis, request, status, response, conclusion, explicit_id
    ):
        record = {
            "id": self._reserve_id(explicit_id),
            "seq": seq,
            "x_req": x_req,
            "hipotesis": hipotesis,
            "request": request,
            "status": status,
            "response": response,
        }
        if conclusion is not None:
            record["conclusion"] = conclusion
        return record

    def _append(self, record):
        self._records.append(record)

    def record(
        self,
        seq,
        x_req,
        hipotesis,
        request,
        status,
        response,
        conclusion=None,
        id=None,
    ):
        """Appends a line. Rejects an ``id`` already present instead of duplicating."""
        record = self._build(
            seq, x_req, hipotesis, request, status, response, conclusion, id
        )
        self._append(record)
        return record["id"]

    def accepted_controls(self):
        """Map ``payload_of -> control`` of every already-accepted control."""
        result = {}
        for record in self._records:
            request = record.get("request")
            if not isinstance(request, dict):
                continue
            base, control = payload_of(request.get("m"))
            if base is None:
                continue
            if is_control_accepted(record.get("response")):
                result[base] = control
        return result

    def accepted_control(self, payload):
        return self.accepted_controls().get(payload)


class ExperimentLog(ExperimentLogBase):
    """On-disk log. It continues the ids from whatever already exists."""

    def __init__(self, path=DEFAULT_PATH):
        super().__init__()
        self.path = Path(path)
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            record_id = record.get("id")
            if isinstance(record_id, int):
                self._ids.add(record_id)
                if record_id > self._max_id:
                    self._max_id = record_id
            self._records.append(record)

    def _append(self, record):
        line = json.dumps(record, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
        self._records.append(record)


class InMemoryLog(ExperimentLogBase):
    """In-memory log for tests and dry-run. It does not touch the disk."""


class ReadOnlyLog:
    """Dry-run wrapper: delegates reads and discards writes."""

    def __init__(self, base):
        self._base = base

    def accepted_controls(self):
        return self._base.accepted_controls()

    def accepted_control(self, payload):
        return self._base.accepted_control(payload)

    def record(self, *args, **kwargs):
        return None
