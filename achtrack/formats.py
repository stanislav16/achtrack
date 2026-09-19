"""Decoders for less common self-describing save formats. Each returns plain Python data
(dicts, lists, scalars) or raises ValueError when the bytes aren't in that format."""
from __future__ import annotations

import base64
import plistlib
import re
import sqlite3
import struct

MAX_ITEMS = 5_000


# --- .NET BinaryFormatter (MS-NRBF), used by many Unity games ---------------------------------------

PRIMITIVE = {1: "?", 2: "B", 6: "d", 7: "h", 8: "i", 9: "q", 10: "b", 11: "f", 12: "q", 13: "q", 14: "H", 15: "I", 16: "Q"}


class _NRBF:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.objects: dict = {}
        self.classes: dict = {}

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise ValueError("truncated")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u(self, fmt: str):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def lps(self) -> str:
        length, shift = 0, 0
        for _ in range(5):
            byte = self.u("B")
            length |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        if length > 1_000_000:
            raise ValueError("bad string")
        return self.take(length).decode("utf-8", "replace")

    def primitive(self, kind: int):
        if kind == 18:
            return self.lps()
        if kind == 5:
            return self.lps()
        if kind == 3:
            first = self.data[self.pos]
            n = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
            return self.take(n).decode("utf-8", "replace")
        if kind == 17:
            return None
        if kind not in PRIMITIVE:
            raise ValueError(f"primitive {kind}")
        value = self.u(PRIMITIVE[kind])
        return value & 0x3FFFFFFFFFFFFFFF if kind == 13 else value

    def class_info(self):
        object_id = self.u("i")
        name = self.lps()
        count = self.u("i")
        if not 0 <= count < 10_000:
            raise ValueError("bad member count")
        return object_id, name, [self.lps() for _ in range(count)]

    def member_types(self, count: int) -> list:
        kinds = [self.u("B") for _ in range(count)]
        extra = []
        for kind in kinds:
            if kind in (0, 7):
                extra.append(self.u("B"))
            elif kind == 3:
                extra.append(self.lps())
            elif kind == 4:
                extra.append((self.lps(), self.u("i")))
            else:
                extra.append(None)
        return list(zip(kinds, extra))

    def read_class(self, object_id: int, name: str, members: list, types: list | None) -> dict:
        obj: dict = {"__type__": name}
        self.objects[object_id] = obj
        for i, member in enumerate(members):
            if types and types[i][0] == 0:
                value = self.primitive(types[i][1])
            else:
                value = self.record()
            obj[re.sub(r"^<(.+)>k__BackingField$", r"\1", member)] = value
        return obj

    def read_values(self, count: int, kind: int | None = None, prim: int | None = None) -> list:
        out: list = []
        while len(out) < count:
            if kind == 0 or prim is not None:
                out.append(self.primitive(prim))
                continue
            value = self.record()
            if isinstance(value, _Nulls):
                out.extend([None] * value.n)
            else:
                out.append(value)
        return out[:MAX_ITEMS]

    def record(self):
        kind = self.u("B")
        if kind == 0:
            self.take(16)
            return self.record()
        if kind == 12:
            self.u("i")
            self.lps()
            return self.record()
        if kind in (5, 4):
            object_id, name, members = self.class_info()
            types = self.member_types(len(members))
            if kind == 5:
                self.u("i")
            self.classes[object_id] = (name, members, types)
            return self.read_class(object_id, name, members, types)
        if kind in (3, 2):
            object_id, name, members = self.class_info()
            if kind == 3:
                self.u("i")
            self.classes[object_id] = (name, members, None)
            return self.read_class(object_id, name, members, None)
        if kind == 1:
            object_id, meta = self.u("i"), self.u("i")
            if meta not in self.classes:
                raise ValueError("unknown class reference")
            name, members, types = self.classes[meta]
            return self.read_class(object_id, name, members, types)
        if kind == 6:
            object_id = self.u("i")
            value = self.lps()
            self.objects[object_id] = value
            return value
        if kind == 7:
            object_id, array_type, rank = self.u("i"), self.u("B"), self.u("i")
            if not 0 < rank < 8:
                raise ValueError("bad rank")
            lengths = [self.u("i") for _ in range(rank)]
            if array_type in (3, 4, 5):
                [self.u("i") for _ in range(rank)]
            (elem_kind, extra), = self.member_types(1)
            total = 1
            for n in lengths:
                total *= max(n, 0)
            if total > 10_000_000:
                raise ValueError("array too big")
            values = self.read_values(total, elem_kind, extra if elem_kind in (0,) else None)
            self.objects[object_id] = values
            return values
        if kind == 8:
            return self.primitive(self.u("B"))
        if kind == 9:
            return _Ref(self.u("i"))
        if kind == 10:
            return None
        if kind == 11:
            return _End()
        if kind == 13:
            return _Nulls(self.u("B"))
        if kind == 14:
            return _Nulls(self.u("i"))
        if kind == 15:
            object_id, length, prim = self.u("i"), self.u("i"), self.u("B")
            if not 0 <= length < 10_000_000:
                raise ValueError("bad array")
            if prim == 2:
                values = list(self.take(length)[:MAX_ITEMS])
            else:
                values = self.read_values(length, prim=prim)
            self.objects[object_id] = values
            return values
        if kind in (16, 17):
            object_id, length = self.u("i"), self.u("i")
            if not 0 <= length < 10_000_000:
                raise ValueError("bad array")
            values = self.read_values(length)
            self.objects[object_id] = values
            return values
        raise ValueError(f"record type {kind}")


class _Ref:
    def __init__(self, target: int):
        self.target = target


class _Nulls:
    def __init__(self, n: int):
        self.n = n


class _End:
    pass


def _resolve(value, objects: dict, depth: int = 0, seen: frozenset = frozenset()):
    if depth > 40:
        return None
    if isinstance(value, _Ref):
        if value.target in seen:
            return None
        return _resolve(objects.get(value.target), objects, depth + 1, seen | {value.target})
    if isinstance(value, list):
        return [_resolve(v, objects, depth + 1, seen) for v in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        kind = str(value.get("__type__", ""))
        body = {k: _resolve(v, objects, depth + 1, seen) for k, v in value.items() if k != "__type__"}
        # Unwrap .NET collections into plain lists/dicts.
        if kind.startswith("System.Collections.Generic.List") and "_items" in body:
            items = body.get("_items") or []
            return items[:body.get("_size", len(items))]
        if kind.startswith("System.Collections.Generic.Dictionary") and "KeyValuePairs" in body:
            pairs = body.get("KeyValuePairs") or []
            return {str(p.get("key")): p.get("value") for p in pairs if isinstance(p, dict)}
        if kind.startswith("System.Collections.Generic.HashSet") and "Elements" in body:
            return body.get("Elements") or []
        return body
    return value


def is_nrbf(data: bytes) -> bool:
    return len(data) > 17 and data[0] == 0 and data[5:9] == b"\xff\xff\xff\xff" and data[9:13] == b"\x01\0\0\0"


def parse_nrbf(data: bytes):
    reader = _NRBF(data)
    reader.take(1)
    root_id = reader.u("i")
    reader.take(12)
    first = None
    while reader.pos < len(data):
        value = reader.record()
        if isinstance(value, _End):
            break
        if first is None:
            first = value
    root = reader.objects.get(root_id, first)
    return _resolve(root, reader.objects)


# --- BSON -------------------------------------------------------------------------------------------

def parse_bson(data: bytes):
    if len(data) < 5 or struct.unpack("<i", data[:4])[0] != len(data) or data[-1] != 0:
        raise ValueError("not BSON")
    value, _ = _bson_doc(data, 0)
    return value


def _bson_doc(data: bytes, pos: int, as_list: bool = False):
    size = struct.unpack_from("<i", data, pos)[0]
    end = pos + size
    pos += 4
    out: dict = {}
    while pos < end - 1:
        kind = data[pos]
        pos += 1
        key_end = data.index(b"\0", pos)
        key = data[pos:key_end].decode("utf-8", "replace")
        pos = key_end + 1
        if kind == 0x01:
            value = struct.unpack_from("<d", data, pos)[0]; pos += 8
        elif kind == 0x02:
            n = struct.unpack_from("<i", data, pos)[0]
            value = data[pos + 4:pos + 3 + n].decode("utf-8", "replace"); pos += 4 + n
        elif kind in (0x03, 0x04):
            value, pos = _bson_doc(data, pos, kind == 0x04)
        elif kind == 0x05:
            n = struct.unpack_from("<i", data, pos)[0]; value = None; pos += 5 + n
        elif kind == 0x07:
            value = None; pos += 12
        elif kind == 0x08:
            value = bool(data[pos]); pos += 1
        elif kind in (0x09, 0x11, 0x12):
            value = struct.unpack_from("<q", data, pos)[0]; pos += 8
        elif kind == 0x0A:
            value = None
        elif kind == 0x10:
            value = struct.unpack_from("<i", data, pos)[0]; pos += 4
        elif kind == 0x13:
            value = None; pos += 16
        else:
            raise ValueError(f"BSON type {kind}")
        out[key] = value
    result = [out[k] for k in sorted(out, key=lambda k: int(k) if k.isdigit() else 0)] if as_list else out
    return result, end


# --- MessagePack --------------------------------------------------------------------------------------

def parse_msgpack(data: bytes):
    value, pos = _mp(data, 0, 0)
    if pos < len(data) * 0.98 or not isinstance(value, (dict, list)):
        raise ValueError("not MessagePack")
    sample = value if isinstance(value, dict) else next((v for v in value if isinstance(v, dict)), {})
    if len([k for k in sample if isinstance(k, str) and k.isprintable()]) < 3:
        raise ValueError("MessagePack without field names")
    return value


def _mp(d: bytes, p: int, depth: int):
    if depth > 60:
        raise ValueError("too deep")
    b = d[p]
    p += 1
    if b <= 0x7F:
        return b, p
    if b >= 0xE0:
        return b - 0x100, p
    if 0x80 <= b <= 0x8F:
        return _mp_map(d, p, b & 0x0F, depth)
    if 0x90 <= b <= 0x9F:
        return _mp_arr(d, p, b & 0x0F, depth)
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        return d[p:p + n].decode("utf-8", "replace"), p + n
    simple = {0xC0: None, 0xC2: False, 0xC3: True}
    if b in simple:
        return simple[b], p
    fixed = {0xCA: ">f", 0xCB: ">d", 0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
             0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
    if b in fixed:
        fmt = fixed[b]
        return struct.unpack_from(fmt, d, p)[0], p + struct.calcsize(fmt)
    lengths = {0xC4: ">B", 0xC5: ">H", 0xC6: ">I", 0xD9: ">B", 0xDA: ">H", 0xDB: ">I"}
    if b in lengths:
        fmt = lengths[b]
        n = struct.unpack_from(fmt, d, p)[0]
        p += struct.calcsize(fmt)
        chunk = d[p:p + n]
        if p + n > len(d):
            raise ValueError("truncated")
        return (chunk.decode("utf-8", "replace") if b >= 0xD9 else None), p + n
    if b in (0xDC, 0xDD):
        fmt = ">H" if b == 0xDC else ">I"
        n = struct.unpack_from(fmt, d, p)[0]
        return _mp_arr(d, p + struct.calcsize(fmt), n, depth)
    if b in (0xDE, 0xDF):
        fmt = ">H" if b == 0xDE else ">I"
        n = struct.unpack_from(fmt, d, p)[0]
        return _mp_map(d, p + struct.calcsize(fmt), n, depth)
    ext = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}
    if b in ext:
        return None, p + 1 + ext[b]
    if b in (0xC7, 0xC8, 0xC9):
        fmt = {0xC7: ">B", 0xC8: ">H", 0xC9: ">I"}[b]
        n = struct.unpack_from(fmt, d, p)[0]
        return None, p + struct.calcsize(fmt) + 1 + n
    raise ValueError("bad MessagePack byte")


def _mp_arr(d: bytes, p: int, n: int, depth: int):
    if n > 10_000_000:
        raise ValueError("array too big")
    out = []
    for _ in range(n):
        v, p = _mp(d, p, depth + 1)
        if len(out) < MAX_ITEMS:
            out.append(v)
    return out, p


def _mp_map(d: bytes, p: int, n: int, depth: int):
    if n > 10_000_000:
        raise ValueError("map too big")
    out = {}
    for _ in range(n):
        k, p = _mp(d, p, depth + 1)
        v, p = _mp(d, p, depth + 1)
        out[str(k)] = v
    return out, p


# --- RPG Maker MV (LZString.compressToBase64) ---------------------------------------------------------

_LZ_KEY = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def lzstring_base64(text: str) -> str:
    text = text.strip()
    if not text or len(text) % 4 not in (0, 2, 3) or re.search(r"[^A-Za-z0-9+/=]", text):
        raise ValueError("not LZString base64")
    lookup = {c: i for i, c in enumerate(_LZ_KEY)}
    values = [lookup[c] for c in text]
    pos, bits_per_char = 0, 6
    val, position = values[0], 32

    def bits(n: int) -> int:
        nonlocal val, position, pos
        out, power = 0, 1
        for _ in range(n):
            resb = val & position
            position >>= 1
            if position == 0:
                position = 32
                pos += 1
                val = values[pos] if pos < len(values) else 0
            out |= (1 if resb > 0 else 0) * power
            power <<= 1
        return out

    dictionary = {0: 0, 1: 1, 2: 2}
    enlarge, dict_size, num_bits = 4, 4, 3
    first = bits(2)
    if first == 2:
        return ""
    c = chr(bits(8 if first == 0 else 16))
    dictionary[3] = c
    w, result = c, [c]
    for _ in range(10_000_000):
        if pos > len(values):
            raise ValueError("truncated LZString")
        code = bits(num_bits)
        if code in (0, 1):
            dictionary[dict_size] = chr(bits(8 if code == 0 else 16))
            code = dict_size
            dict_size += 1
            enlarge -= 1
        elif code == 2:
            return "".join(result)
        if enlarge == 0:
            enlarge = 1 << num_bits
            num_bits += 1
        if code in dictionary:
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            raise ValueError("bad LZString")
        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge -= 1
        w = entry
        if enlarge == 0:
            enlarge = 1 << num_bits
            num_bits += 1
    raise ValueError("LZString too long")


# --- containers ----------------------------------------------------------------------------------------

def parse_sqlite(path: str) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        out: dict = {}
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                             "AND name NOT LIKE 'sqlite_%' LIMIT 60")]
        for table in tables:
            cur = conn.execute(f'SELECT * FROM "{table}" LIMIT 5000')
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            lowered = [c.lower() for c in cols]
            key_col = next((i for i, c in enumerate(lowered) if c in ("key", "name", "id", "k")), None)
            if key_col is not None and len(cols) <= 3:
                value_col = next(i for i in range(len(cols)) if i != key_col)
                out[table] = {str(r[key_col]): r[value_col] for r in rows}
            else:
                out[table] = [{c: v for c, v in zip(cols, r) if not isinstance(v, bytes)} for r in rows]
        return out
    finally:
        conn.close()


def parse_plist(data: bytes):
    if not (data.startswith(b"bplist00") or b"<plist" in data[:400]):
        raise ValueError("not a plist")
    return plistlib.loads(data)


def unity_linux_prefs(root) -> dict:
    """~/.config/unity3d/<Company>/<Game>/prefs: <pref name=".." type="int|float|string">"""
    out = {}
    for pref in root.iter("pref"):
        name, kind, text = pref.get("name"), pref.get("type"), (pref.text or "").strip()
        if not name:
            continue
        value = text
        if kind == "string":
            try:
                value = base64.b64decode(text).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                pass
        elif kind in ("int", "float"):
            try:
                value = int(text) if kind == "int" else float(text)
            except ValueError:
                pass
        out[name] = value
    return out
