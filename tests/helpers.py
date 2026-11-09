"""Original, permissioned sample data. Generated descriptors never enter Git."""
from dataclasses import replace
import hashlib
from kura.models import Release
from kura.torrent import encode

PAYLOAD = b"Kura original authorized test payload. CC0.\n" * 1000
NAME = "[Kura] Kura Sample - 03 (1080p).txt"


def sample(name=NAME, payload=PAYLOAD):
    info = {b"name": name.encode(), b"length": len(payload), b"piece length": 16384,
            b"pieces": b"".join(hashlib.sha1(payload[i:i+16384]).digest()
                               for i in range(0, len(payload), 16384))}
    ih = hashlib.sha1(encode(info)).hexdigest()
    data = encode({b"announce": b"http://evil.invalid/announce",
                   b"url-list": [b"http://127.0.0.1/private"], b"info": info})
    row = Release("1", name, "Kura Sample", 3, "Kura", "1080p", "english",
                  len(payload), 10, ih, "https://nyaa.si/download/1.torrent", True)
    return row, data


class FakeProvider:
    def __init__(self, rows=None, data=None):
        row, descriptor = sample()
        self.rows = rows if rows is not None else [row]
        self.data = data if data is not None else descriptor

    def search(self, title, episode, subtitles):
        return self.rows

    def descriptor(self, release):
        return self.data
