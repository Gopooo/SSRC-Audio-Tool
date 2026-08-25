import argparse
import array
import math
import os
import re
import shutil
import struct
import sys
import wave
import threading
import queue
import time
import csv
import concurrent.futures
from collections import defaultdict
from pathlib import Path

HDR = struct.Struct(">IHHII")


# ---------------------------------------------------------------------------
# WAV conversion
# ---------------------------------------------------------------------------

def _pcm_to_channels(path):
    """Read uncompressed WAV -> (channels[list[int]], rate). Supports 8/16/24/32-bit PCM."""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        rate = w.getframerate()
        sw = w.getsampwidth()
        comp = w.getcomptype()
        n = w.getnframes()
        raw = w.readframes(n)

    if comp != "NONE":
        raise ValueError("The WAV must be uncompressed PCM.")
    if ch < 1:
        raise ValueError("Invalid WAV channel count.")

    vals = []
    if sw == 1:
        # WAV PCM8 is unsigned.
        vals = [(b - 128) << 8 for b in raw]
    elif sw == 2:
        vals = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    elif sw == 3:
        for i in range(0, len(raw) - 2, 3):
            v = raw[i] | (raw[i+1] << 8) | (raw[i+2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            vals.append(max(-32768, min(32767, v >> 8)))
    elif sw == 4:
        iv = struct.unpack("<%di" % (len(raw) // 4), raw)
        vals = [max(-32768, min(32767, v >> 16)) for v in iv]
    else:
        raise ValueError(f"PCM {sw*8} bit PCM is not supported.")

    chans = [vals[c::ch] for c in range(ch)]
    return chans, rate


def _match_channels(chans, target_ch):
    src_ch = len(chans)
    if src_ch == target_ch:
        return chans

    n = min(len(c) for c in chans)
    if target_ch == 1:
        # Average all source channels.
        out = []
        for i in range(n):
            out.append(int(sum(c[i] for c in chans) / src_ch))
        return [out]

    if src_ch == 1 and target_ch == 2:
        return [list(chans[0]), list(chans[0])]

    raise ValueError(f"Conversion {src_ch} -> {target_ch} channel conversion is not supported.")


def _resample_channel(samples, src_rate, dst_rate):
    if src_rate == dst_rate:
        return list(samples)
    if not samples:
        return []

    out_n = max(1, int(round(len(samples) * dst_rate / src_rate)))
    if len(samples) == 1:
        return [samples[0]] * out_n

    scale = src_rate / dst_rate
    out = [0] * out_n
    last = len(samples) - 1
    for i in range(out_n):
        x = i * scale
        j = int(x)
        if j >= last:
            out[i] = int(samples[last])
        else:
            frac = x - j
            v = samples[j] * (1.0 - frac) + samples[j+1] * frac
            out[i] = max(-32768, min(32767, int(round(v))))
    return out


def match_new_audio(original_wav, new_wav):
    ref_chans, ref_rate = _pcm_to_channels(original_wav)
    new_chans, new_rate = _pcm_to_channels(new_wav)

    target_ch = len(ref_chans)
    new_chans = _match_channels(new_chans, target_ch)
    new_chans = [_resample_channel(c, new_rate, ref_rate) for c in new_chans]

    # Keep channel lengths identical.
    n = min(len(c) for c in new_chans)
    new_chans = [c[:n] for c in new_chans]

    return new_chans, ref_rate, target_ch, new_rate, len(_pcm_to_channels(new_wav)[0])


# ---------------------------------------------------------------------------
# Streams2.dat parsing
# ---------------------------------------------------------------------------

def children(f, off, size):
    out = []
    p, end = off + 16, off + 16 + size
    while p + 16 <= end:
        f.seek(p)
        h = f.read(16)
        if len(h) < 16:
            break
        idw, ver, flags, w2, sz = HDR.unpack(h)
        if not (idw & 0x80000000) or p + 16 + sz > end:
            break
        out.append((idw & 0x7FFFFFFF, p, sz, flags, ver))
        p += 16 + sz
    return out


def parse_records(src):
    f = open(src, "rb")
    idw, ver, flags, w2, rootsize = HDR.unpack(f.read(16))
    if (idw & 0x7FFFFFFF) != 0x11F8:
        f.close()
        raise ValueError("This file does not look like a Wii Streams2.dat (root != 0x11F8).")

    rows = []
    for cid, off, size, fl, v in children(f, 0, rootsize):
        if cid != 0x11FB:
            continue
        p, end = off + 16, off + 16 + size
        while p + 16 <= end:
            f.seek(p)
            h = f.read(16)
            if len(h) < 16:
                break
            idw2, v2, f2, w22, s2 = HDR.unpack(h)
            if not (idw2 & 0x80000000):
                break
            d = f.read(s2)
            for i in range(0, s2, 32):
                if i + 32 > s2:
                    break
                u = struct.unpack_from(">8I", d, i)
                rows.append((p + 16 + i, {
                    "off": u[1],
                    "id": u[2],
                    "pad": u[5],
                    "len": u[6],
                }))
            p += 16 + s2
    f.close()
    return rows


def locate_media(f, off, pad, dlen):
    cands = [off + 32 + pad]

    f.seek(off)
    idw, ver, fl, w2, size = HDR.unpack(f.read(16))
    p, pend = off + 16, off + 16 + size

    while p + 16 <= pend:
        f.seek(p)
        i2, v2, f2, w22, s2 = HDR.unpack(f.read(16))
        if not (i2 & 0x80000000):
            break
        if (i2 & 0x7FFFFFFF) == 0x1200:
            cands.append(p + 16 + pad)
        p += 16 + s2

    for c in cands:
        f.seek(c)
        if f.read(4) == b"RIFX":
            return c

    raise ValueError("Media RIFX not found.")


# ---------------------------------------------------------------------------
# Wwise RIFX
# ---------------------------------------------------------------------------

def rifx_chunks(blob):
    if blob[:4] != b"RIFX" or blob[8:12] != b"WAVE":
        raise ValueError("Invalid Wwise RIFX.")

    total = struct.unpack(">I", blob[4:8])[0]
    p, end = 12, min(8 + total, len(blob))
    out = []

    while p + 8 <= end:
        cid = blob[p:p+4]
        sz = struct.unpack(">I", blob[p+4:p+8])[0]
        if p + 8 + sz > end:
            break
        out.append((cid, p, sz, blob[p+8:p+8+sz]))
        p += 8 + sz + (sz & 1)

    return out


def parse_fmt(blob):
    # Prefer normal chunk parsing.
    fmt = None
    for cid, p, sz, d in rifx_chunks(blob):
        if cid == b"fmt ":
            fmt = d
            break

    # Relaxed fallback for these Wwise files.
    if fmt is None:
        p = blob.find(b"fmt ", 12)
        if p >= 0 and p + 8 <= len(blob):
            sz = struct.unpack(">I", blob[p+4:p+8])[0]
            if p + 8 + sz <= len(blob):
                fmt = blob[p+8:p+8+sz]

    if fmt is None or len(fmt) < 18:
        raise ValueError("fmt chunk not found.")

    tag, ch, rate, brate, balign, bits = struct.unpack(">HHIIHH", fmt[:16])

    if tag != 2 or bits != 4:
        raise ValueError(f"Unsupported codec : tag={tag}, bits={bits}")

    ext = fmt[18:]
    coefs = []
    for c in range(ch):
        base = 10 + c * 46
        if base + 32 > len(ext):
            raise ValueError("Incomplete DSP coefficients.")
        coefs.append(struct.unpack_from(">16h", ext, base))

    return {"ch": ch, "rate": rate, "coefs": coefs}


# ---------------------------------------------------------------------------
# Wii DSP-ADPCM encoder
# ---------------------------------------------------------------------------

def clamp16(x):
    return max(-32768, min(32767, int(x)))


def _simulate_frame(s14, coefs, pred, shift, h1, h2):
    """Encode/simulate one predictor+shift candidate. Returns (error,nibbles,h1,h2)."""
    c1, c2 = coefs[pred*2], coefs[pred*2+1]
    lh1, lh2 = h1, h2
    nibbles = []
    err = 0
    step = 1 << shift

    for target in s14:
        predicted = (c1 * lh1 + c2 * lh2 + 1024) >> 11
        q = int(round((target - predicted) / step))
        q = max(-8, min(7, q))
        recon = clamp16(predicted + (q << shift))
        e = target - recon
        err += e * e
        nibbles.append(q & 15)
        lh2, lh1 = lh1, recon

    return err, nibbles, lh1, lh2


def encode_frame(samples, coefs, h1, h2):
    """
    Faster version of the validated encoder.

    Instead of brute-forcing all 13 shifts for every predictor, estimate the
    useful shift from the prediction residual, then test only that shift and
    its immediate neighbors. This keeps the same DSP-ADPCM format while making
    long voice replacements much faster.
    """
    s14 = list(samples) + [0] * (14 - len(samples))
    best = None
    npred = min(8, len(coefs) // 2)

    for pred in range(npred):
        c1, c2 = coefs[pred*2], coefs[pred*2+1]

        # Estimate residual range without mutating history.
        lh1, lh2 = h1, h2
        max_abs = 0
        for target in s14:
            predicted = (c1 * lh1 + c2 * lh2 + 1024) >> 11
            residual = target - predicted
            if abs(residual) > max_abs:
                max_abs = abs(residual)
            # Use target as a cheap history approximation for shift estimation.
            lh2, lh1 = lh1, target

        # Need residual/2^shift to fit roughly in signed 4-bit [-8,7].
        if max_abs <= 7:
            est = 0
        else:
            est = max(0, min(12, int(math.ceil(math.log(max_abs / 7.0, 2)))))

        candidates = sorted(set(max(0, min(12, est + d)) for d in (-1, 0, 1, 2)))
        for shift in candidates:
            err, nibs, nh1, nh2 = _simulate_frame(
                s14, coefs, pred, shift, h1, h2
            )
            if best is None or err < best[0]:
                best = (err, pred, shift, nibs, nh1, nh2)

    _, pred, shift, nibs, nh1, nh2 = best
    out = bytearray([(pred << 4) | shift])
    for i in range(0, 14, 2):
        out.append((nibs[i] << 4) | nibs[i+1])

    return bytes(out), nh1, nh2


def encode_channel(samples, coefs, progress=None, progress_base=0.0, progress_span=1.0):
    out = bytearray()
    h1 = h2 = 0
    total_frames = max(1, (len(samples) + 13) // 14)
    report_every = max(1, total_frames // 200)

    for fi, i in enumerate(range(0, len(samples), 14)):
        frame, h1, h2 = encode_frame(samples[i:i+14], coefs, h1, h2)
        out += frame

        if progress and (fi % report_every == 0 or fi + 1 == total_frames):
            frac = (fi + 1) / total_frames
            progress(progress_base + frac * progress_span)

    return bytes(out)

def interleave_frames(lanes):
    if len(lanes) == 1:
        return lanes[0]

    counts = [len(x) // 8 for x in lanes]
    nf = max(counts)
    out = bytearray()
    zero = b"\0" * 8

    for i in range(nf):
        for lane in lanes:
            if (i + 1) * 8 <= len(lane):
                out += lane[i*8:(i+1)*8]
            else:
                out += zero

    return bytes(out)


# ---------------------------------------------------------------------------
# RIFX duration patch + rebuild
# ---------------------------------------------------------------------------

def rebuild_rifx(original, new_audio, sample_count):
    chunks = rifx_chunks(original)

    data_pos = None
    old_size = None
    fmt_pos = None
    smpl_pos = None

    for cid, p, sz, d in chunks:
        if cid == b"fmt ":
            fmt_pos = p
        elif cid == b"smpl":
            smpl_pos = p
        elif cid == b"data":
            data_pos, old_size = p, sz

    if fmt_pos is None:
        q = original.find(b"fmt ", 12)
        if q >= 0:
            fmt_pos = q

    if smpl_pos is None:
        q = original.find(b"smpl", 12)
        if q >= 0:
            smpl_pos = q

    if data_pos is None:
        p0 = 12
        while True:
            p = original.find(b"data", p0)
            if p < 0:
                break
            if p + 8 <= len(original):
                sz = struct.unpack(">I", original[p+4:p+8])[0]
                if p + 8 + sz <= len(original):
                    data_pos, old_size = p, sz
                    break
            p0 = p + 1

    if data_pos is None:
        raise ValueError("RIFX data chunk not found.")

    work = bytearray(original)
    sample_count = int(sample_count)

    # fmt payload +0x18 = total PCM sample count for this SSCR Wii DSP stream.
    if fmt_pos is None or fmt_pos + 8 + 0x1C > len(work):
        raise ValueError("fmt sample-count field not found.")

    fmt_size = struct.unpack(">I", work[fmt_pos+4:fmt_pos+8])[0]
    if fmt_size < 0x1C:
        raise ValueError("fmt chunk is too small.")

    old_samples = struct.unpack_from(">I", work, fmt_pos + 8 + 0x18)[0]
    struct.pack_into(">I", work, fmt_pos + 8 + 0x18, sample_count)

    old_loop_end = None
    new_loop_end = max(0, sample_count - 1)

    if smpl_pos is not None and smpl_pos + 8 <= len(work):
        smpl_size = struct.unpack(">I", work[smpl_pos+4:smpl_pos+8])[0]
        payload = smpl_pos + 8
        if smpl_size >= 60 and payload + smpl_size <= len(work):
            loops = struct.unpack_from(">I", work, payload + 28)[0]
            if loops >= 1:
                old_loop_end = struct.unpack_from(">I", work, payload + 48)[0]
                struct.pack_into(">I", work, payload + 48, new_loop_end)

    original = bytes(work)

    old_end = data_pos + 8 + old_size + (old_size & 1)
    if old_end > len(original):
        raise ValueError("Original data chunk is truncated.")

    rebuilt = bytearray()
    rebuilt += original[:data_pos]
    rebuilt += b"data" + struct.pack(">I", len(new_audio)) + new_audio

    if len(new_audio) & 1:
        rebuilt += b"\0"

    rebuilt += original[old_end:]
    rebuilt[4:8] = struct.pack(">I", len(rebuilt) - 8)

    return bytes(rebuilt), old_samples, old_loop_end, new_loop_end


# ---------------------------------------------------------------------------
# Repack
# ---------------------------------------------------------------------------

def read_chunk_header(f, off):
    f.seek(off)
    idw, ver, flags, w2, size = HDR.unpack(f.read(16))
    return {"idw": idw, "ver": ver, "flags": flags, "w2": w2, "size": size}


def pack_header(h, size=None):
    return HDR.pack(
        h["idw"], h["ver"], h["flags"], h["w2"],
        h["size"] if size is None else size
    )


def find_top_chunk(src, want):
    with open(src, "rb") as f:
        root = read_chunk_header(f, 0)
        for cid, off, size, fl, ver in children(f, 0, root["size"]):
            if cid == want:
                return root, off, size
    return None


def clone_stream_with_replacement(src, stream_off, rebuilt_rifx, new_stream_off):
    with open(src, "rb") as f:
        parent = read_chunk_header(f, stream_off)
        kids = children(f, stream_off, parent["size"])

        if not kids:
            raise ValueError("The original 0x11FE has no children.")

        parts = []
        found_media = False
        new_pad = None
        cursor_abs = new_stream_off + 16

        for cid, off, size, fl, ver in kids:
            h = read_chunk_header(f, off)
            f.seek(off + 16)
            payload = f.read(size)

            if cid == 0x1200:
                found_media = True
                payload_start_abs = cursor_abs + 16
                new_pad = (-payload_start_abs) % 0x8000
                new_payload = (b"\xBA" * new_pad) + rebuilt_rifx
                part = pack_header(h, len(new_payload)) + new_payload
            else:
                part = pack_header(h) + payload

            parts.append(part)
            cursor_abs += len(part)

        if not found_media:
            raise ValueError("The original 0x11FE has no 0x1200 child.")

        body = b"".join(parts)
        return pack_header(parent, len(body)) + body, new_pad


def parse_media_id_from_filename(path):
    stem = Path(path).stem
    m = re.search(r"_([0-9a-fA-F]{8})(?:_MATCHED|_MOD)?$", stem)
    if not m:
        # More relaxed: last 8 hex chars anywhere near end.
        ms = re.findall(r"([0-9a-fA-F]{8})", stem)
        if not ms:
            return None
        return int(ms[-1], 16)
    return int(m.group(1), 16)


def mod_audio(streams_path, original_wav, replacement_wav, out_path=None, media_id=None, log=print, progress=None):
    streams_path = Path(streams_path)
    original_wav = Path(original_wav)
    replacement_wav = Path(replacement_wav)
    out_path = Path(out_path) if out_path else streams_path.with_name(streams_path.stem + "_mod" + streams_path.suffix)

    if media_id is None:
        media_id = parse_media_id_from_filename(original_wav)
    if media_id is None:
        raise ValueError(
            "Could not find the Media ID in the original WAV filename. "
            "Use a filename such as 2031_v13_1c97de6d.wav or provide --id."
        )

    log(f"Media ID : 0x{media_id:08X}")
    log("Reading audio settings...")

    # Reference WAV dictates target sample rate and channels.
    ref_chans, ref_rate = _pcm_to_channels(original_wav)
    new_chans, new_rate = _pcm_to_channels(replacement_wav)

    target_ch = len(ref_chans)
    new_chans = _match_channels(new_chans, target_ch)
    new_chans = [_resample_channel(c, new_rate, ref_rate) for c in new_chans]
    nsamples = min(len(c) for c in new_chans)
    new_chans = [c[:nsamples] for c in new_chans]

    log(
        f"Conversion: {len(_pcm_to_channels(replacement_wav)[0])} ch / {new_rate} Hz "
        f"-> {target_ch} ch / {ref_rate} Hz"
    )
    log(f"Replacement audio: {nsamples} samples ({nsamples/ref_rate:.3f} s)")

    entries = [(eo, e) for eo, e in parse_records(streams_path) if e["id"] == media_id]
    if not entries:
        raise ValueError(f"Media ID 0x{media_id:08X} was not found in Streams2.dat.")

    phys = {(e["off"], e["pad"], e["len"]) for _, e in entries}
    if len(phys) != 1:
        raise ValueError("The same Media ID points to multiple physical streams.")

    _, olde = entries[0]

    with open(streams_path, "rb") as f:
        old_rifx_start = locate_media(f, olde["off"], olde["pad"], olde["len"])
        f.seek(old_rifx_start)
        original_rifx = f.read(olde["len"])

    fmt = parse_fmt(original_rifx)

    if fmt["ch"] != target_ch:
        raise ValueError(
            f"The extracted original WAV reports {target_ch} channel(s), "
            f"but the RIFX reports {fmt['ch']}."
        )
    if fmt["rate"] != ref_rate:
        raise ValueError(
            f"The extracted original WAV reports {ref_rate} Hz, "
            f"but the RIFX reports {fmt['rate']} Hz."
        )

    log("Encoding Wii DSP-ADPCM...")
    lanes = []
    for c in range(fmt["ch"]):
        log(f"Encoding channel {c+1}/{fmt['ch']}...")
        base = c / fmt["ch"]
        span = 1.0 / fmt["ch"]
        lane = encode_channel(
            new_chans[c], fmt["coefs"][c],
            progress=progress,
            progress_base=base,
            progress_span=span,
        )
        lanes.append(lane)
    encoded = interleave_frames(lanes)

    rebuilt, old_samples, old_loop_end, new_loop_end = rebuild_rifx(
        original_rifx, encoded, nsamples
    )

    log(f"fmt duration: {old_samples} -> {nsamples} samples")
    if old_loop_end is not None:
        log(f"smpl end  : {old_loop_end} -> {new_loop_end}")

    found = find_top_chunk(streams_path, 0x11FD)
    if not found:
        raise ValueError("0x11FD container not found.")

    root_h, s11fd_off, s11fd_size = found

    with open(streams_path, "rb") as f:
        topkids = children(f, 0, root_h["size"])

    if not topkids or topkids[-1][0] != 0x11FD or topkids[-1][1] != s11fd_off:
        raise ValueError("0x11FD is not the final root child; repack refused.")

    new_stream_off = s11fd_off + 16 + s11fd_size
    new_stream_bytes, new_pad = clone_stream_with_replacement(
        streams_path, olde["off"], rebuilt, new_stream_off
    )

    log(f"Creating {out_path.name}...")
    shutil.copy2(streams_path, out_path)

    with open(out_path, "r+b") as f:
        # Append new stream.
        f.seek(new_stream_off)
        f.write(new_stream_bytes)

        # Grow 0x11FD.
        new_11fd_size = s11fd_size + len(new_stream_bytes)
        f.seek(s11fd_off + 12)
        f.write(struct.pack(">I", new_11fd_size))

        # Grow root.
        new_root_size = root_h["size"] + len(new_stream_bytes)
        f.seek(12)
        f.write(struct.pack(">I", new_root_size))

        # Redirect all references for this media ID.
        for entry_file_off, e in entries:
            f.seek(entry_file_off + 4)
            f.write(struct.pack(">I", new_stream_off))
            f.seek(entry_file_off + 20)
            f.write(struct.pack(">I", new_pad))
            f.seek(entry_file_off + 24)
            f.write(struct.pack(">I", len(rebuilt)))

    log("")
    log("=== COMPLETE ===")
    log(f"Media ID           : 0x{media_id:08X}")
    log(f"Original allocation: {olde['len']} bytes")
    log(f"New RIFX           : {len(rebuilt)} bytes")
    log(f"Original stream     : 0x{olde['off']:X}")
    log(f"New stream          : 0x{new_stream_off:X}")
    log(f"Padding            : {new_pad} bytes")
    log(f"Output              : {out_path}")
    log("The source Streams2.dat was NOT modified.")

    return out_path



# ---------------------------------------------------------------------------
# Audio extraction + SoundBank organization
# ---------------------------------------------------------------------------

def pkz_header(buf, off):
    idw, ver, flags, w2, size = struct.unpack_from(">IHHII", buf, off)
    return idw & 0x7FFFFFFF, ver, flags, size


def pkz_children(buf, off, size):
    out = []
    p, end = off + 16, off + 16 + size
    while p + 16 <= end:
        idw, ver, flags, w2, csize = struct.unpack_from(">IHHII", buf, p)
        if not (idw & 0x80000000) or p + 16 + csize > end:
            break
        out.append((idw & 0x7FFFFFFF, p, csize, flags, ver))
        p += 16 + csize
    return out


def pkz_top(buf):
    cid, ver, flags, size = pkz_header(buf, 0)
    return pkz_children(buf, 0, size)


def pkz_find_child(kids, want):
    return next((k for k in kids if k[0] == want), None)


def pkz_asset_header(buf, off138e):
    p = off138e + 16
    h, atype = struct.unpack_from(">II", buf, p)
    name = buf[p + 28:p + 28 + 64].split(b"\0")[0].decode("latin1")
    return h, atype, name


def pkz_assets(buf):
    out = []
    for cid, off, size, flags, ver in pkz_top(buf):
        if cid != 0x26:
            continue
        kids = pkz_children(buf, off, size)
        if not kids or kids[0][0] != 0x138E:
            continue
        h, atype, name = pkz_asset_header(buf, kids[0][1])
        out.append((name, atype, h, off, size, kids))
    return out


def stream_table_name(mask, langids):
    if mask == 0xFFFFFFFF:
        return "neutral"
    for i in range(32):
        if mask == (1 << i):
            lid = langids[i] if i < len(langids) else -1
            return "lang%02d_id%d" % (i, lid)
    return "mask%08x" % mask


def parse_stream_tables(src):
    """
    Returns (language_ids, tables, records).
    tables = [(mask, [(stream_index, record_file_offset), ...]), ...]
    records = {record_chunk_offset: [variation_entry, ...]}
    """
    f = open(src, "rb")
    try:
        f.seek(0)
        idw, ver, flags, w2, rootsize = struct.unpack(">IHHII", f.read(16))
        if (idw & 0x7FFFFFFF) != 0x11F8:
            raise ValueError("Selected file is not a Wii Streams2.dat.")

        kids = children(f, 0, rootsize)
        langids, tables, records = [], [], {}

        for cid, off, size, fl, v in kids:
            if cid == 0x0019:
                f.seek(off + 16)
                langids = list(struct.unpack(">%dI" % (size // 4), f.read(size)))

            elif cid == 0x11F9:
                for c2, o2, s2, f2, v2 in children(f, off, size):
                    if c2 != 0x11FA:
                        continue
                    f.seek(o2 + 16)
                    d = f.read(s2)
                    mask = struct.unpack_from(">I", d, 0)[0]
                    pairs = [
                        struct.unpack_from(">II", d, 4 + i * 8)
                        for i in range((s2 - 4) // 8)
                    ]
                    tables.append((mask, pairs))

            elif cid == 0x11FB:
                p, end = off + 16, off + 16 + size
                while p + 16 <= end:
                    f.seek(p)
                    idw2, v2, f2, w22, s2 = struct.unpack(">IHHII", f.read(16))
                    if not (idw2 & 0x80000000):
                        break
                    d = f.read(s2)
                    ents = []
                    for i in range(0, s2, 32):
                        if i + 32 > len(d):
                            break
                        u = struct.unpack_from(">8I", d, i)
                        ents.append({
                            "off": u[1],
                            "id": u[2],
                            "pad": u[5],
                            "len": u[6],
                        })
                    records[p] = ents
                    p += 16 + s2

        return langids, tables, records
    finally:
        f.close()


def dsp_decode(data, coefs):
    """Decode one Wii/GameCube DSP-ADPCM channel to signed PCM16 samples."""
    out = []
    append = out.append
    h1 = h2 = 0

    # One DSP frame = 8 bytes -> 14 PCM samples.
    for frame_off in range(0, len(data) - 7, 8):
        head = data[frame_off]
        shift = head & 0x0F
        pred = (head >> 4) & 0x0F

        # SSCR streams use the standard 8 predictor pairs.
        if pred * 2 + 1 >= len(coefs):
            raise ValueError(f"Invalid DSP predictor index {pred}")

        c1 = coefs[pred * 2]
        c2 = coefs[pred * 2 + 1]

        for b in data[frame_off + 1:frame_off + 8]:
            for nib in (b >> 4, b & 0x0F):
                if nib > 7:
                    nib -= 16

                sample = (
                    ((nib << shift) << 11)
                    + 1024
                    + c1 * h1
                    + c2 * h2
                ) >> 11

                if sample > 32767:
                    sample = 32767
                elif sample < -32768:
                    sample = -32768

                append(sample)
                h2 = h1
                h1 = sample

    return out


def decode_stream_rifx(blob):
    r = {}
    if blob[:4] != b"RIFX" or blob[8:12] != b"WAVE":
        raise ValueError("No RIFX")

    total = struct.unpack(">I", blob[4:8])[0]
    p, end = 12, min(8 + total, len(blob))

    while p + 8 <= end:
        cid = blob[p:p + 4]
        csz = struct.unpack(">I", blob[p + 4:p + 8])[0]
        if p + 8 + csz > len(blob):
            break
        d = blob[p + 8:p + 8 + csz]

        if cid == b"fmt ":
            tag, ch, rate, brate, balign, bits = struct.unpack(">HHIIHH", d[:16])
            r.update(tag=tag, ch=ch, rate=rate, balign=balign, bits=bits)
            if tag == 2 and bits == 4:
                ext = d[18:]
                coefs = []
                for c in range(ch):
                    base2 = 10 + c * 46
                    coefs.append(struct.unpack_from(">16h", ext, base2))
                r["coefs"] = coefs

        elif cid == b"data":
            r["data"] = d

        p += 8 + csz + (csz & 1)

    if r.get("tag") != 2 or "coefs" not in r or "data" not in r:
        raise ValueError("Unsupported or incomplete DSP-ADPCM RIFX")

    audio, ch = r["data"], r["ch"]

    if ch == 1:
        chans = [dsp_decode(audio, r["coefs"][0])]
    else:
        lanes = [bytearray() for _ in range(ch)]
        step = 8 * ch
        for i in range(0, len(audio) - step + 1, step):
            for c in range(ch):
                lanes[c] += audio[i + c * 8:i + (c + 1) * 8]
        chans = [dsp_decode(bytes(lanes[c]), r["coefs"][c]) for c in range(ch)]

    return chans, r["rate"]


def write_pcm16_wav(path, chans, rate):
    n = min(len(c) for c in chans)
    ch = len(chans)
    inter = array.array("h", [0] * (n * ch))
    for c, dat in enumerate(chans):
        inter[c::ch] = array.array("h", dat[:n])
    if sys.byteorder == "big":
        inter.byteswap()
    pcm = inter.tobytes()

    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack(
            "<IHHIIHH",
            16, 1, ch, rate, rate * ch * 2, ch * 2, 16
        ))
        f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)


def extraction_locate_blob(src, off, pad, dlen):
    with open(src, "rb") as f:
        cands = [off + 32 + pad]

        f.seek(off)
        idw, ver, fl, w2, size = struct.unpack(">IHHII", f.read(16))
        p, pend = off + 16, off + 16 + size

        while p + 16 <= pend:
            f.seek(p)
            i2, v2, f2, ww2, s2 = struct.unpack(">IHHII", f.read(16))
            if not (i2 & 0x80000000):
                break
            if (i2 & 0x7FFFFFFF) == 0x1200:
                cands.append(p + 16 + pad)
            p += 16 + s2

        for c in cands:
            f.seek(c)
            if f.read(4) == b"RIFX":
                f.seek(c)
                return f.read(dlen)

    raise ValueError("RIFX not found")


def extract_one_stream(job):
    src, outp, off, pad, dlen = job
    try:
        blob = extraction_locate_blob(src, off, pad, dlen)
        chans, rate = decode_stream_rifx(blob)
        write_pcm16_wav(outp + ".wav", chans, rate)
        return outp, "ok"
    except Exception as e:
        return outp, "FAIL " + str(e)[:100]


def build_extraction_jobs(streams_path, output_root):
    langids, tables, records = parse_stream_tables(streams_path)

    output_root = Path(output_root)
    wav_root = output_root / "audio"
    wav_root.mkdir(parents=True, exist_ok=True)

    jobs = []
    manifest = []
    written = {}

    for ti, (mask, pairs) in enumerate(tables):
        tname = stream_table_name(mask, langids)
        tdir = wav_root / tname
        tdir.mkdir(parents=True, exist_ok=True)

        for index, recoff in pairs:
            ents = records.get(recoff)
            if not ents:
                continue

            for vi, e in enumerate(ents):
                key = (e["off"], e["pad"])
                base_name = "%04d%s_%08x" % (
                    index,
                    "_v%d" % vi if len(ents) > 1 else "",
                    e["id"],
                )
                outp = str(tdir / base_name)

                row = {
                    "table": tname,
                    "index": index,
                    "var": vi,
                    "id": "%08x" % e["id"],
                    "off": e["off"],
                    "bytes": e["len"],
                    "file": base_name,
                    "dup_of": "",
                }

                if key in written:
                    row["dup_of"] = written[key]
                else:
                    written[key] = base_name
                    jobs.append((
                        str(streams_path), outp,
                        e["off"], e["pad"], e["len"]
                    ))

                manifest.append(row)

    return langids, tables, records, jobs, manifest, wav_root


def iter_bank_blobs(data_dir, progress=None):
    pkzs = sorted(
        p for p in Path(data_dir).iterdir()
        if p.is_file() and p.suffix.lower() == ".pkz"
    )
    total = max(1, len(pkzs))

    for pi, path in enumerate(pkzs):
        try:
            buf = path.read_bytes()
        except Exception:
            if progress:
                progress((pi + 1) / total)
            continue

        # 0x0044 bank section path.
        try:
            for cid, off, size, flags, ver in pkz_top(buf):
                if cid != 0x0044:
                    continue

                for c1, o1, s1, f1, v1 in pkz_children(buf, off, size):
                    if c1 != 0x138D:
                        continue

                    sub = pkz_children(buf, o1, s1)
                    e138e = pkz_find_child(sub, 0x138E)
                    e1130 = pkz_find_child(sub, 0x1130)
                    if not e138e or not e1130:
                        continue

                    h, atype, nm = pkz_asset_header(buf, e138e[1])
                    e1131 = pkz_find_child(
                        pkz_children(buf, e1130[1], e1130[2]),
                        0x1131
                    )
                    if e1131:
                        yield nm, buf[e1131[1] + 16:e1131[1] + 16 + e1131[2]]

            # 0x0026 type-10 assets.
            for name, atype, h, off, size, kids in pkz_assets(buf):
                if atype != 10:
                    continue
                e1130 = pkz_find_child(kids, 0x1130)
                if not e1130:
                    continue
                e1131 = pkz_find_child(
                    pkz_children(buf, e1130[1], e1130[2]),
                    0x1131
                )
                if e1131:
                    yield name, buf[e1131[1] + 16:e1131[1] + 16 + e1131[2]]
        finally:
            if progress:
                progress((pi + 1) / total)


def u32_values_all_byte_alignments(blob):
    vals = set()
    for sh in range(4):
        n = (len(blob) - sh) // 4
        if n <= 0:
            continue
        view = blob[sh:sh + n * 4]
        vals.update(v[0] for v in struct.iter_unpack(">I", view))
    return vals


def bank_group(bankset):
    groups = sorted(re.sub(r"^Col_SndBnk\w*?_", "", b) for b in bankset)
    return groups[0] if groups else ""


def organize_extracted_audio(wav_root, id2banks, log=print):
    gmap = {i: bank_group(b) for i, b in id2banks.items()}
    moved = 0

    for dirpath, dirnames, filenames in os.walk(wav_root):
        # Do not recursively re-process files we already moved into a group folder.
        rel = Path(dirpath).relative_to(wav_root)
        if len(rel.parts) >= 2:
            continue

        for fn in filenames:
            m = re.match(r".*_([0-9a-fA-F]{8})\.wav$", fn)
            if not m:
                continue

            mid = int(m.group(1), 16)
            group = gmap.get(mid)
            if not group:
                continue

            safe = re.sub(r'[<>:"/\\|?*]', "_", group)[:100]
            dst_dir = Path(dirpath) / safe
            dst_dir.mkdir(parents=True, exist_ok=True)

            src = Path(dirpath) / fn
            dst = dst_dir / fn

            if dst.exists():
                dst.unlink()
            src.replace(dst)
            moved += 1

    log(f"Organized {moved} WAV file(s) into bank/group folders.")
    return moved


def extract_and_organize(data_dir, output_root, log=print, progress=None, workers=None):
    """
    Full user-facing pipeline:
      Data folder -> Streams2.dat -> WAV + manifest -> SoundBank mapping -> folders.
    """
    data_dir = Path(data_dir)
    output_root = Path(output_root)

    streams_path = data_dir / "Streams2.dat"
    if not streams_path.exists():
        raise ValueError(
            "Streams2.dat was not found directly inside the selected Data folder."
        )

    output_root.mkdir(parents=True, exist_ok=True)

    log(f"Streams2.dat: {streams_path}")
    log("Reading stream tables...")

    langids, tables, records, jobs, manifest, wav_root = build_extraction_jobs(
        streams_path, output_root
    )

    log(f"Language tables: {len(tables)}")
    log(f"Unique media to extract: {len(jobs)}")
    log(f"Manifest references: {len(manifest)}")

    manifest_path = output_root / "streams_manifest.csv"
    with manifest_path.open("w", newline="", encoding="utf-8") as fcsv:
        w = csv.DictWriter(
            fcsv,
            fieldnames=["table", "index", "var", "id", "off", "bytes", "file", "dup_of"]
        )
        w.writeheader()
        w.writerows(manifest)

    if workers is None:
        workers = max(1, (os.cpu_count() or 2) - 1)

    log(f"Extracting WAV files with {workers} worker process(es)...")

    done = failed = 0
    total_jobs = max(1, len(jobs))

    # ProcessPool keeps DSP decoding fast without freezing the GUI.
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(extract_one_stream, j) for j in jobs]
        for fut in concurrent.futures.as_completed(futures):
            outp, status = fut.result()
            done += 1
            if status.startswith("FAIL"):
                failed += 1
                log(f"{status}: {Path(outp).name}")

            if progress:
                # Extraction is the first 70% of the operation.
                progress(0.70 * done / total_jobs)

            if done % 250 == 0 or done == len(jobs):
                log(f"Extracted {done}/{len(jobs)} unique media...")

    log(f"WAV extraction complete: {len(jobs)-failed} OK, {failed} failed.")
    if failed:
        log(
            "Some streams could not be decoded. They remain listed in the manifest; "
            "successful WAV files will still be organized."
        )
    log("Scanning PKZ SoundBanks to identify speaker/context...")

    ids = {int(r["id"], 16) for r in manifest}
    id2banks = defaultdict(set)
    bank_count = 0

    def bank_progress(frac):
        if progress:
            progress(0.70 + 0.25 * frac)

    for nm, blob in iter_bank_blobs(data_dir, progress=bank_progress):
        bank_count += 1
        found = u32_values_all_byte_alignments(blob) & ids
        for mid in found:
            id2banks[mid].add(nm)

        if bank_count % 500 == 0:
            log(f"Scanned {bank_count} SoundBanks...")

    log(f"SoundBanks collected: {bank_count}")
    log(f"Media IDs attributed: {len(id2banks)}/{len(ids)}")

    names_path = output_root / "stream_names.csv"
    with names_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "group", "banks"])
        for mid in sorted(id2banks):
            bs = sorted(id2banks[mid])
            w.writerow(["%08x" % mid, bank_group(bs), ";".join(bs)])

    log("Organizing WAV files...")
    organize_extracted_audio(wav_root, id2banks, log=log)

    if progress:
        progress(1.0)

    wav_count = sum(1 for _ in wav_root.rglob("*.wav"))
    log("")
    if wav_count:
        log("=== EXTRACTION COMPLETE ===")
    else:
        log("=== EXTRACTION FINISHED WITH NO WAV OUTPUT ===")
    log(f"WAV files    : {wav_count}")
    log(f"Audio folder : {wav_root}")
    log(f"Manifest     : {manifest_path}")
    log(f"Bank mapping : {names_path}")
    log(
        "Note: Wwise does not store the original streamed source filenames here; "
        "the tool uses SoundBank names to identify speaker/context."
    )

    return {
        "audio_root": wav_root,
        "manifest": manifest_path,
        "names": names_path,
        "streams": streams_path,
    }


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui():
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as e:
        print("Tkinter unavailable:", e)
        print(__doc__)
        return 2

    root = tk.Tk()
    root.title("Skylanders SuperChargers Racing Audio Tool")
    root.geometry("900x760")
    root.minsize(820, 680)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    # Shared UI queue: worker threads/process orchestration never touch Tk directly.
    ui_queue = queue.Queue()

    # ------------------------------------------------------------------
    # EXTRACT & ORGANIZE TAB
    # ------------------------------------------------------------------
    extract_tab = ttk.Frame(notebook)
    notebook.add(extract_tab, text="Extract & Organize")

    ex_data = tk.StringVar()
    ex_output = tk.StringVar()
    ex_status = tk.StringVar(value="Ready")
    ex_progress = tk.DoubleVar(value=0.0)
    ex_busy = {"value": False}

    ttk.Label(
        extract_tab,
        text="Extract and organize Streams2.dat audio",
        font=("Arial", 13, "bold")
    ).pack(pady=(14, 4))

    ttk.Label(
        extract_tab,
        text=(
            "Select the game's Data folder. The tool will extract every streamed "
            "Wwise voice/audio file to WAV, create the manifests, scan the PKZ "
            "SoundBanks, and organize the WAVs by speaker/context."
        ),
        wraplength=790,
        justify="center"
    ).pack(padx=16, pady=(0, 12))

    def ex_path_row(label, variable, browse_cmd):
        frame = ttk.Frame(extract_tab)
        frame.pack(fill="x", padx=14, pady=6)
        ttk.Label(frame, text=label, width=20).pack(side="left")
        ttk.Entry(frame, textvariable=variable).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(frame, text="Browse...", command=browse_cmd).pack(side="right")

    def choose_data():
        p = filedialog.askdirectory(title="Select the game Data folder")
        if p:
            ex_data.set(p)
            if not ex_output.get():
                ex_output.set(str(Path(p).parent / "Extracted_Audio"))

    def choose_extract_output():
        p = filedialog.askdirectory(title="Select extraction output folder")
        if p:
            ex_output.set(p)

    ex_path_row("Game Data folder", ex_data, choose_data)
    ex_path_row("Output folder", ex_output, choose_extract_output)

    ex_status_frame = ttk.Frame(extract_tab)
    ex_status_frame.pack(fill="x", padx=14, pady=(10, 3))
    ttk.Label(ex_status_frame, textvariable=ex_status).pack(anchor="w")
    ttk.Progressbar(
        ex_status_frame,
        maximum=100,
        variable=ex_progress,
        mode="determinate"
    ).pack(fill="x", pady=(4, 0))

    ex_button = ttk.Button(extract_tab, text="EXTRACT & ORGANIZE AUDIO")
    ex_button.pack(fill="x", padx=14, pady=(10, 6))

    ex_log = scrolledtext.ScrolledText(extract_tab, height=16)
    ex_log.pack(fill="both", expand=True, padx=14, pady=(4, 10))

    def ex_log_worker(msg):
        ui_queue.put(("extract_log", str(msg)))

    def ex_progress_worker(frac):
        ui_queue.put(("extract_progress", float(frac)))

    def start_extract():
        if ex_busy["value"]:
            return

        data = ex_data.get().strip()
        out = ex_output.get().strip()
        if not data or not out:
            messagebox.showerror(
                "Error",
                "Select the game Data folder and an output folder."
            )
            return

        if not (Path(data) / "Streams2.dat").exists():
            messagebox.showerror(
                "Streams2.dat not found",
                "The selected folder must directly contain Streams2.dat and the game's PKZ files."
            )
            return

        ex_busy["value"] = True
        ex_button.config(state="disabled")
        ex_log.delete("1.0", "end")
        ex_progress.set(0)
        ex_status.set("Starting extraction...")

        def worker():
            try:
                result = extract_and_organize(
                    data, out,
                    log=ex_log_worker,
                    progress=ex_progress_worker,
                )
                ui_queue.put(("extract_done", result))
            except Exception as e:
                ui_queue.put(("extract_error", e))

        threading.Thread(target=worker, daemon=True).start()

    ex_button.config(command=start_extract)

    # ------------------------------------------------------------------
    # REPLACE AUDIO TAB
    # ------------------------------------------------------------------
    replace_tab = ttk.Frame(notebook)
    notebook.add(replace_tab, text="Replace Audio")

    vars_ = {
        "streams": tk.StringVar(),
        "original": tk.StringVar(),
        "replacement": tk.StringVar(),
        "output": tk.StringVar(),
    }

    rep_status = tk.StringVar(value="Ready")
    rep_progress = tk.DoubleVar(value=0.0)
    rep_busy = {"value": False}

    ttk.Label(
        replace_tab,
        text="Replace audio inside Streams2.dat",
        font=("Arial", 13, "bold")
    ).pack(pady=(14, 4))

    ttk.Label(
        replace_tab,
        text=(
            "Select a Streams2.dat source, an extracted WAV whose filename contains "
            "the Media ID, and any replacement WAV. The replacement is converted, "
            "DSP-ADPCM encoded, duration metadata is updated, and a new Streams2 file "
            "is created. Successful edits are automatically chained."
        ),
        wraplength=790,
        justify="center"
    ).pack(padx=16, pady=(0, 12))

    def rep_row(label, key, filetypes):
        frame = ttk.Frame(replace_tab)
        frame.pack(fill="x", padx=14, pady=6)

        ttk.Label(frame, text=label, width=22).pack(side="left")
        ttk.Entry(frame, textvariable=vars_[key]).pack(
            side="left", fill="x", expand=True, padx=6
        )

        def browse():
            p = filedialog.askopenfilename(filetypes=filetypes)
            if p:
                vars_[key].set(p)
                if key == "streams" and not vars_["output"].get():
                    sp = Path(p)
                    vars_["output"].set(
                        str(sp.with_name(sp.stem + "_mod" + sp.suffix))
                    )

        ttk.Button(frame, text="Browse...", command=browse).pack(side="right")

    rep_row("Streams2.dat source", "streams", [("DAT", "*.dat"), ("All files", "*.*")])
    rep_row("Extracted original WAV", "original", [("WAV", "*.wav"), ("All files", "*.*")])
    rep_row("Replacement WAV", "replacement", [("WAV", "*.wav"), ("All files", "*.*")])

    out_frame = ttk.Frame(replace_tab)
    out_frame.pack(fill="x", padx=14, pady=6)
    ttk.Label(out_frame, text="Output file", width=22).pack(side="left")
    ttk.Entry(out_frame, textvariable=vars_["output"]).pack(
        side="left", fill="x", expand=True, padx=6
    )

    def browse_rep_output():
        p = filedialog.asksaveasfilename(
            defaultextension=".dat",
            filetypes=[("DAT", "*.dat"), ("All files", "*.*")]
        )
        if p:
            vars_["output"].set(p)

    ttk.Button(out_frame, text="Browse...", command=browse_rep_output).pack(side="right")

    rep_status_frame = ttk.Frame(replace_tab)
    rep_status_frame.pack(fill="x", padx=14, pady=(10, 3))
    ttk.Label(rep_status_frame, textvariable=rep_status).pack(anchor="w")
    ttk.Progressbar(
        rep_status_frame,
        maximum=100,
        variable=rep_progress,
        mode="determinate"
    ).pack(fill="x", pady=(4, 0))

    rep_button = ttk.Button(replace_tab, text="CREATE MODIFIED STREAMS2.DAT")
    rep_button.pack(fill="x", padx=14, pady=(10, 6))

    rep_log = scrolledtext.ScrolledText(replace_tab, height=14)
    rep_log.pack(fill="both", expand=True, padx=14, pady=(4, 10))

    def rep_log_worker(msg):
        ui_queue.put(("replace_log", str(msg)))

    def rep_progress_worker(frac):
        ui_queue.put(("replace_progress", float(frac)))

    def start_replace():
        if rep_busy["value"]:
            return

        streams = vars_["streams"].get().strip()
        original = vars_["original"].get().strip()
        replacement = vars_["replacement"].get().strip()
        output = vars_["output"].get().strip() or None

        if not streams or not original or not replacement:
            messagebox.showerror("Error", "Select all three required files.")
            return

        mid = parse_media_id_from_filename(original)
        if mid is None:
            messagebox.showerror(
                "Media ID not found",
                "The extracted original WAV filename must contain an 8-digit hexadecimal Media ID,\n"
                "for example: 2031_v13_1c97de6d.wav"
            )
            return

        rep_busy["value"] = True
        rep_button.config(state="disabled")
        rep_log.delete("1.0", "end")
        rep_progress.set(0)
        rep_status.set("Starting replacement...")

        def worker():
            try:
                out = mod_audio(
                    streams,
                    original,
                    replacement,
                    out_path=output,
                    media_id=mid,
                    log=rep_log_worker,
                    progress=rep_progress_worker,
                )
                ui_queue.put(("replace_done", out))
            except Exception as e:
                ui_queue.put(("replace_error", e))

        threading.Thread(target=worker, daemon=True).start()

    rep_button.config(command=start_replace)

    # ------------------------------------------------------------------
    # Queue handling
    # ------------------------------------------------------------------
    def append(box, msg):
        box.insert("end", str(msg) + "\n")
        box.see("end")

    def poll_queue():
        try:
            while True:
                kind, value = ui_queue.get_nowait()

                if kind == "extract_log":
                    append(ex_log, value)
                    ex_status.set(value if len(value) < 90 else value[:87] + "...")

                elif kind == "extract_progress":
                    ex_progress.set(max(0, min(100, value * 100)))
                    ex_status.set(f"Extracting / organizing... {value*100:.1f}%")

                elif kind == "extract_done":
                    ex_busy["value"] = False
                    ex_button.config(state="normal")
                    ex_progress.set(100)
                    ex_status.set("Complete")

                    # Pre-fill replacer from extraction result.
                    vars_["streams"].set(str(value["streams"]))
                    if not vars_["output"].get():
                        sp = Path(value["streams"])
                        vars_["output"].set(
                            str(sp.with_name(sp.stem + "_mod" + sp.suffix))
                        )

                    messagebox.showinfo(
                        "Extraction complete",
                        "Audio extraction and SoundBank organization finished successfully.\n\n"
                        f"Audio folder:\n{value['audio_root']}\n\n"
                        "The Replace Audio tab has also been pre-filled with Streams2.dat."
                    )

                elif kind == "extract_error":
                    ex_busy["value"] = False
                    ex_button.config(state="normal")
                    ex_status.set("Error")
                    append(ex_log, "")
                    append(ex_log, "ERROR: " + str(value))
                    messagebox.showerror("Extraction error", str(value))

                elif kind == "replace_log":
                    append(rep_log, value)
                    if value.startswith("Encoding channel"):
                        rep_status.set(value)
                    elif value.startswith("Creating "):
                        rep_status.set(value)

                elif kind == "replace_progress":
                    rep_progress.set(max(0, min(100, value * 100)))
                    rep_status.set(f"Encoding Wii DSP-ADPCM... {value*100:.1f}%")

                elif kind == "replace_done":
                    rep_busy["value"] = False
                    rep_button.config(state="normal")
                    rep_progress.set(100)
                    rep_status.set("Complete")

                    # Multi-edit chaining.
                    vars_["streams"].set(str(value))
                    op = Path(value)
                    if re.search(r"_mod\d*$", op.stem):
                        m = re.search(r"_mod(\d*)$", op.stem)
                        num = int(m.group(1) or "1") + 1
                        next_stem = re.sub(r"_mod\d*$", f"_mod{num}", op.stem)
                        next_out = op.with_name(next_stem + op.suffix)
                    else:
                        next_out = op.with_name(op.stem + "_mod2" + op.suffix)

                    vars_["output"].set(str(next_out))

                    messagebox.showinfo(
                        "Complete",
                        f"Audio replaced successfully.\n\nCreated file:\n{value}\n\n"
                        "This file is now the Streams2 source for the next replacement."
                    )

                elif kind == "replace_error":
                    rep_busy["value"] = False
                    rep_button.config(state="normal")
                    rep_status.set("Error")
                    append(rep_log, "")
                    append(rep_log, "ERROR: " + str(value))
                    messagebox.showerror("Replacement error", str(value))

        except queue.Empty:
            pass

        root.after(60, poll_queue)

    root.after(60, poll_queue)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) == 1:
        return run_gui()

    ap = argparse.ArgumentParser(description="Skylanders Streams2 Audio Modder")
    ap.add_argument("streams")
    ap.add_argument("original_wav")
    ap.add_argument("replacement_wav")
    ap.add_argument("--out")
    ap.add_argument("--id", dest="media_id")
    args = ap.parse_args()

    mid = int(args.media_id, 0) if args.media_id else None

    mod_audio(
        args.streams,
        args.original_wav,
        args.replacement_wav,
        out_path=args.out,
        media_id=mid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
