"""Best-effort text redaction for synthetic model fixtures, not private archives."""

import json
import os
from pathlib import Path
import re


class Redactor:
    def __init__(self, patterns=(), *, secrets=(), home=None):
        values = set(secrets)
        values.update(value for name, value in os.environ.items() if value and any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET")))
        self._secrets = sorted((v for v in values if isinstance(v, str) and v), key=len, reverse=True)
        self._home = str(home or Path.home())
        self.patterns = [
            re.compile(r"\bsk-[A-Za-z0-9_-]+"),
            re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
            re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
            *(re.compile(p) for p in patterns),
        ]
        self.count = 0

    def text(self, text):
        # Placeholders are not scanned again: short environment secrets must
        # not corrupt the placeholders emitted for longer secrets.
        pieces = [(part, part in {"<SECRET>", "<HOME>", "<REDACTED>"})
                  for part in re.split(r"(<SECRET>|<HOME>|<REDACTED>)", text)]
        rules = [(re.compile(re.escape(value)), "<SECRET>") for value in self._secrets]
        if self._home:
            homes = {self._home, self._home.replace("\\", "/"), self._home.replace("/", "\\")}
            rules.extend((re.compile(re.escape(h), re.I), "<HOME>") for h in sorted(homes, key=len, reverse=True))
        rules.extend((p, "<REDACTED>") for p in self.patterns)
        for pattern, replacement in rules:
            result = []
            for part, protected in pieces:
                if protected:
                    result.append((part, True))
                    continue
                offset = 0
                for match in pattern.finditer(part):
                    result.extend([(part[offset:match.start()], False), (replacement, True)])
                    offset = match.end()
                    self.count += 1
                result.append((part[offset:], False))
            pieces = result
        return "".join(part for part, _ in pieces)

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                redacted = self.text(key)
                if redacted in result:
                    raise ValueError("redaction causes duplicate object keys")
                result[redacted] = self.value(item)
            return result
        return value

    def assert_safe(self, text):
        # Parse JSON before scanning so escaped Windows paths are recognized.
        for line in text.splitlines():
            parsed = json.loads(line)
            before = self.count
            self.value(parsed)
            if self.count != before:
                raise ValueError("cassette contains unredacted sensitive text")
