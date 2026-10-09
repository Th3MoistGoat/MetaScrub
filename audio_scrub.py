"""
audio_scrub.py - pure-Python metadata strippers for audio formats that
ExifTool 13.59 cannot write (MP3/ID3, FLAC, Ogg Vorbis/Opus).

Each strip function takes file bytes and returns (new_bytes, notes).
They raise ValueError on invalid input. Only stdlib is used.
"""

import struct

# ---------------------------------------------------------------------------
# MP3 / ID3
# ---------------------------------------------------------------------------

def _syncsafe_to_int(b4):
    return ((b4[0] & 0x7F) << 21) | ((b4[1] & 0x7F) << 14) | \
           ((b4[2] & 0x7F) << 7) | (b4[3] & 0x7F)


def strip_id3_tags(data: bytes):
    """Remove ID3v2 (front), ID3v1 / APEv2 / Lyrics3v2 (end) tags."""
    notes = []
    orig_len = len(data)
    if orig_len < 16:
        return data, ["file too small, left alone"]

    # --- ID3v2 at start: "ID3" + ver(2) + flags(1) + syncsafe size(4) ---
    if data[:3] == b"ID3" and len(data) >= 10:
        size = _syncsafe_to_int(data[6:10])
        if 0 < size < len(data):
            end = 10 + size
            if data[5] & 0x10:  # footer present flag
                end += 10
            if end <= len(data):
                notes.append("removed ID3v2 tag (%d bytes)" % end)
                data = data[end:]

    # --- APEv2 at start: "APETAGEX" header ---
    if len(data) >= 32 and data[:8] == b"APETAGEX":
        size = struct.unpack("<I", data[12:16])[0]
        if 0 < size <= len(data):
            notes.append("removed APEv2 tag (%d bytes)" % size)
            data = data[size:]

    # --- end tags: ID3v1 is always last, then APEv2 footer, then Lyrics3 ---
    if len(data) >= 128 and data[-128:-125] == b"TAG":
        if len(data) >= 355 and data[-355:-351] == b"TAG+":
            notes.append("removed ID3v1 + extended tag (355 bytes)")
            data = data[:-355]
        else:
            notes.append("removed ID3v1 tag (128 bytes)")
            data = data[:-128]

    if len(data) >= 32 and data[-32:-24] == b"APETAGEX":
        size = struct.unpack("<I", data[-20:-16])[0]
        if 0 < size <= len(data):
            notes.append("removed APEv2 footer tag (%d bytes)" % size)
            data = data[:-size]

    if len(data) >= 20 and data[-9:] == b"LYRICS200":
        idx = data.rfind(b"LYRICSBEGIN", 0, len(data) - 9)
        if idx != -1 and data[-15:-9].isdigit():
            notes.append("removed Lyrics3v2 tag (%d bytes)" % (len(data) - idx))
            data = data[:idx]

    if not notes:
        notes.append("no ID3/APEv2/Lyrics tags found")
    elif len(data) == orig_len:
        pass
    return data, notes


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------

def strip_flac_tags(data: bytes):
    """Drop Vorbis-comment and picture metadata blocks from FLAC."""
    notes = []
    pos = 0
    # tolerate a leading ID3v2 tag (some FLACs have one)
    if data[:3] == b"ID3" and len(data) >= 10:
        size = _syncsafe_to_int(data[6:10])
        if 0 < size < len(data):
            end = 10 + size + (10 if data[5] & 0x10 else 0)
            if end <= len(data):
                notes.append("removed leading ID3v2 tag (%d bytes)" % end)
                pos = end
    if data[pos:pos + 4] != b"fLaC":
        raise ValueError("not a FLAC file (missing fLaC marker)")
    pos += 4
    blocks = []  # (type, payload)
    while True:
        if pos + 4 > len(data):
            raise ValueError("truncated FLAC metadata")
        header = data[pos]
        last = bool(header & 0x80)
        typ = header & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        payload = data[pos + 4:pos + 4 + length]
        if len(payload) < length:
            raise ValueError("truncated FLAC metadata block")
        blocks.append((typ, payload))
        pos += 4 + length
        if last:
            break
        if len(blocks) > 64:
            raise ValueError("insane FLAC block count")
    audio_start = pos

    if not blocks or blocks[0][0] != 0:
        raise ValueError("FLAC missing STREAMINFO block")

    kept = []
    for typ, payload in blocks:
        if typ == 4:
            notes.append("removed Vorbis comment block (%d bytes)" % len(payload))
        elif typ == 6:
            notes.append("removed picture block (%d bytes)" % len(payload))
        else:
            kept.append((typ, payload))
    if not notes:
        notes.append("no Vorbis-comment/picture blocks found")

    out = bytearray(b"fLaC")
    for i, (typ, payload) in enumerate(kept):
        hdr = (0x80 if i == len(kept) - 1 else 0x00) | typ
        out += bytes([hdr]) + len(payload).to_bytes(3, "big") + payload
    out += data[audio_start:]
    return bytes(out), notes


# ---------------------------------------------------------------------------
# Ogg (Vorbis / Opus / Theora comment packets)
# ---------------------------------------------------------------------------

_OGG_CRC_TABLE = None


def _ogg_crc_table():
    global _OGG_CRC_TABLE
    if _OGG_CRC_TABLE is None:
        t = []
        for i in range(256):
            r = i << 24
            for _ in range(8):
                if r & 0x80000000:
                    r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
                else:
                    r = (r << 1) & 0xFFFFFFFF
            t.append(r)
        _OGG_CRC_TABLE = t
    return _OGG_CRC_TABLE


def _ogg_crc(data: bytes) -> int:
    table = _ogg_crc_table()
    crc = 0
    for b in data:
        crc = table[((crc >> 24) ^ b) & 0xFF] ^ ((crc << 8) & 0xFFFFFFFF)
    return crc


def _ogg_parse(data: bytes):
    """Parse an Ogg stream.

    Returns {"serial", "bos", "eos", "packets"} where packets is a list of
    (packet_bytes, end_granulepos). Raises ValueError on invalid data.
    """
    pages = []
    pos, n = 0, len(data)
    while pos + 27 <= n:
        if data[pos:pos + 4] != b"OggS":
            raise ValueError("bad OggS magic at offset %d" % pos)
        if data[pos + 4] != 0:
            raise ValueError("unsupported Ogg version")
        flags = data[pos + 5]
        granule = struct.unpack("<Q", data[pos + 6:pos + 14])[0]
        serial = struct.unpack("<I", data[pos + 14:pos + 18])[0]
        seq = struct.unpack("<I", data[pos + 18:pos + 22])[0]
        stored_crc = struct.unpack("<I", data[pos + 22:pos + 26])[0]
        nsegs = data[pos + 26]
        segtable = data[pos + 27:pos + 27 + nsegs]
        if len(segtable) < nsegs:
            raise ValueError("truncated segment table")
        body_len = sum(segtable)
        body = data[pos + 27 + nsegs:pos + 27 + nsegs + body_len]
        if len(body) < body_len:
            raise ValueError("truncated page body")
        hdr = bytearray(data[pos:pos + 27 + nsegs])
        hdr[22:26] = b"\x00\x00\x00\x00"
        if _ogg_crc(bytes(hdr) + body) != stored_crc:
            raise ValueError("CRC mismatch on page %d" % seq)
        pages.append({"granule": granule, "serial": serial, "seq": seq,
                      "flags": flags, "segments": list(segtable), "body": body})
        pos += 27 + nsegs + body_len
    if not pages:
        raise ValueError("no Ogg pages found")
    serial0 = pages[0]["serial"]
    packets = []
    cur = bytearray()
    for pg in pages:
        if pg["serial"] != serial0:
            raise ValueError("chained Ogg streams not supported")
        off = 0
        for seg_len in pg["segments"]:
            cur += pg["body"][off:off + seg_len]
            off += seg_len
            if seg_len < 255:
                packets.append((bytes(cur), pg["granule"]))
                cur = bytearray()
    if cur:
        raise ValueError("truncated final packet")
    return {"serial": serial0,
            "bos": bool(pages[0]["flags"] & 0x02),
            "eos": bool(pages[-1]["flags"] & 0x04),
            "packets": packets}


def _ogg_build(serial, packets, bos=True, eos=True):
    """Rebuild an Ogg stream from (packet_bytes, end_granulepos) list."""
    # flatten to segments, tracking packet ends
    segs = []  # (bytes, packet_idx, is_last)
    for pi, (pdata, _) in enumerate(packets):
        if not pdata:
            segs.append((b"", pi, True))
            continue
        off = 0
        while off < len(pdata):
            chunk = pdata[off:off + 255]
            off += len(chunk)
            is_last = off >= len(pdata)
            segs.append((chunk, pi, is_last))
            if is_last and len(chunk) == 255:
                segs.append((b"", pi, True))  # terminator lacing value
    granule_by_packet = {pi: g for pi, (_, g) in enumerate(packets)}
    out = bytearray()
    page_groups = [segs[i:i + 255] for i in range(0, len(segs), 255)] or [[]]
    for pgi, psegs in enumerate(page_groups):
        body = b"".join(s for s, _, _ in psegs)
        segtable = bytes(len(s) for s, _, _ in psegs)
        flags = 0
        if pgi == 0 and bos:
            flags |= 0x02
        if pgi == len(page_groups) - 1 and eos:
            flags |= 0x04
        if pgi > 0 and not page_groups[pgi - 1][-1][2]:
            flags |= 0x01  # continued packet
        granule = 0xFFFFFFFFFFFFFFFF
        for _, pi, is_last in reversed(psegs):
            if is_last:
                granule = granule_by_packet[pi]
                break
        header = (b"OggS" + b"\x00" + bytes([flags]) +
                  struct.pack("<Q", granule) +
                  struct.pack("<I", serial) +
                  struct.pack("<I", pgi) +
                  b"\x00\x00\x00\x00" +
                  bytes([len(segtable)]) + segtable)
        crc = _ogg_crc(header + body)
        header = header[:22] + struct.pack("<I", crc) + header[26:]
        out += header + body
    return bytes(out)


def strip_ogg_tags(data: bytes):
    """Replace the Vorbis/Opus/Theora comment packet with an empty one."""
    notes = []
    parsed = _ogg_parse(data)
    packets = parsed["packets"]
    if len(packets) < 2:
        raise ValueError("not enough Ogg packets for a header")
    p0 = packets[0][0]
    if p0.startswith(b"\x01vorbis"):
        kind, magic = "Vorbis", b"\x03vorbis"
    elif p0.startswith(b"OpusHead"):
        kind, magic = "Opus", b"OpusTags"
    elif p0.startswith(b"\x80theora"):
        kind, magic = "Theora", b"\x81theora"
    else:
        raise ValueError("unsupported Ogg codec")
    old = packets[1][0]
    if not old.startswith(magic):
        raise ValueError("comment packet not where expected")
    new_comment = magic + struct.pack("<II", 0, 0)  # empty vendor, 0 comments
    packets[1] = (new_comment, packets[1][1])
    new_data = _ogg_build(parsed["serial"], packets, parsed["bos"], parsed["eos"])
    # sanity: the rebuilt stream must re-parse to identical packets
    reparsed = _ogg_parse(new_data)
    got = [p for p, _ in reparsed["packets"]]
    want = [p for p, _ in packets]
    if got != want:
        raise ValueError("rebuild verification failed")
    notes.append("removed %s comment packet (%d -> %d bytes)"
                 % (kind, len(old), len(new_comment)))
    return new_data, notes
