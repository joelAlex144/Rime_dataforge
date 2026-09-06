"""Rime /ws3 splits its 1024-byte PCM blocks at arbitrary byte offsets.

Observed in traces/preflight_20260905T163015Z.jsonl: 829+195, 655+369,
1006+18 -- 98 of 1181 chunks were an odd number of bytes, i.e. ended halfway
through an s16le sample. A consumer that decodes each chunk on its own throws
on the odd ones and silently loses both halves of the split block, which is
heard as ~20 ms holes in the speech. The adapter must carry the odd byte into
the next chunk so every yielded chunk is whole samples and alignment is kept.
"""
import asyncio
import base64
import math
import struct
import unittest

from delivery_layer.events import EventLog
from delivery_layer.tts.base import AudioChunk, Done
from delivery_layer.tts.rime import RimeConfig, RimeTTS, _Ctx

# The exact split sizes seen on the wire. Sum = 4096 bytes = 2048 samples.
RIME_SPLITS = [829, 195, 655, 369, 1006, 18, 1024]


def sine_bytes(n_samples: int, hz: float = 440.0, sr: int = 24000) -> bytes:
    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / sr)))
        for i in range(n_samples))


def split(data: bytes, sizes):
    out, at = [], 0
    for n in sizes:
        out.append(data[at:at + n])
        at += n
    assert at == len(data), (at, len(data))
    return out


def drive(chunks: list, cid: str = "c1"):
    """Push raw chunks through RimeTTS._dispatch and drain what it yields."""
    async def go():
        ev = EventLog()
        tts = RimeTTS(RimeConfig(api_key="test", speaker="s"), ev)
        tts._ctx[cid] = _Ctx(tts._generation)
        for raw in chunks:
            tts._dispatch({"type": "chunk", "contextId": cid,
                           "data": base64.b64encode(raw).decode("ascii")})
        tts._dispatch({"type": "done", "contextId": cid})
        q = tts._ctx[cid].queue
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        return ev, items
    return asyncio.new_event_loop().run_until_complete(go())


class TestOddChunkCarry(unittest.TestCase):
    def setUp(self):
        self.original = sine_bytes(2048)
        self.ev, self.items = drive(split(self.original, RIME_SPLITS))
        self.chunks = [i for i in self.items if isinstance(i, AudioChunk)]
        self.done = [i for i in self.items if isinstance(i, Done)]

    def test_every_yielded_chunk_is_whole_samples(self):
        self.assertTrue(self.chunks)
        for c in self.chunks:
            self.assertEqual(len(c.pcm) % 2, 0, f"odd chunk yielded: {len(c.pcm)} bytes")

    def test_total_bytes_preserved_and_counted_from_yield(self):
        total = sum(len(c.pcm) for c in self.chunks)
        self.assertEqual(total, len(self.original))
        self.assertEqual(len(self.done), 1)
        self.assertEqual(self.done[0].total_bytes, total,
                         "Done.total_bytes must count only bytes actually yielded")

    def test_sample_alignment_is_preserved(self):
        """Reassembled audio must equal the original sine byte-for-byte. A
        one-byte shift would decode every sample as garbage."""
        joined = b"".join(c.pcm for c in self.chunks)
        self.assertEqual(joined, self.original)
        decoded = struct.unpack(f"<{len(joined) // 2}h", joined)
        expect = struct.unpack(f"<{len(self.original) // 2}h", self.original)
        self.assertEqual(decoded, expect)

    def test_chunk_realigned_event_counts_the_odd_chunks(self):
        realigned = self.ev.of_type("chunk_realigned")
        self.assertEqual(len(realigned), 1)
        odd = sum(1 for n in RIME_SPLITS if n % 2)
        self.assertEqual(realigned[0]["odd_chunks"], odd)   # 829, 195, 655, 369
        self.assertEqual(realigned[0]["context_id"], "c1")

    def test_no_tail_drop_when_total_is_even(self):
        self.assertEqual(self.ev.of_type("odd_tail_byte_dropped"), [])

    def test_seq_is_contiguous(self):
        self.assertEqual([c.seq for c in self.chunks], list(range(len(self.chunks))))


class TestOddTailByte(unittest.TestCase):
    def test_lone_final_byte_is_dropped_and_reported(self):
        original = sine_bytes(2048) + b"\x7f"          # 4097 bytes: odd total
        ev, items = drive(split(original, [829, 195, 655, 369, 1006, 18, 1025]))
        chunks = [i for i in items if isinstance(i, AudioChunk)]
        done = [i for i in items if isinstance(i, Done)][0]
        joined = b"".join(c.pcm for c in chunks)
        self.assertEqual(joined, original[:-1], "the whole-sample prefix survives intact")
        self.assertEqual(done.total_bytes, len(original) - 1)
        self.assertEqual(len(ev.of_type("odd_tail_byte_dropped")), 1)

    def test_empty_after_carry_is_not_yielded(self):
        """A 1-byte chunk becomes carry only; nothing empty goes downstream."""
        ev, items = drive([b"\x01", b"\x02\x03\x04"])
        chunks = [i for i in items if isinstance(i, AudioChunk)]
        self.assertEqual([len(c.pcm) for c in chunks], [4])
        self.assertEqual(b"".join(c.pcm for c in chunks), b"\x01\x02\x03\x04")


if __name__ == "__main__":
    unittest.main()
