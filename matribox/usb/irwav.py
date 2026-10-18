"""Load cabinet IRs (.wav) into the Matribox II Pro User IR slots, as the PC editor's "Import IR" does.

    irwav.py info cab.wav                                what the file holds and what goes to the pedal
    irwav.py pack cab.wav out.bin [--normalize]          just write the 4 KB slot data
    irwav.py list [--port P]                             names in the 20 User IR slots
    irwav.py send cab.wav --slot N [--name X] [--normalize] [--port P]
    irwav.py rename N name [--port P]

Editor side (ImportIRTask.dart): a WAV that is not 24-bit / 44.1 kHz is first converted by the JUCE code of
HTUSBTools; then the first 4096 bytes of the data chunk are read as 24-bit samples, each one sign-extended into
an int32 (so 1024 samples, 23.2 ms, scale 2^-23, no normalisation) and sent as
0x14000001 {u32 slot, u32 0, u32 checksum, char name[20], data[4096]}. The pedal (FUN_00025d30 with 0x1000 bytes)
stores them in the User IR file system at 0x753000, re-initialises the chain and answers 0x14000003
{slot, 1 = written, 0}. 0x14000002 {u32 slot, char name[16]} renames a slot (no answer). The Cab / User IR engine
(CabIrInit) reads the 1024 int32 straight from flash, left channel only: the same format as the factory cabs.

Here the conversion is done in Python: any PCM / float WAV, the first channel, resampled to 44.1 kHz with a
windowed sinc, a short fade-out when the IR is longer than 1024 samples, optional peak normalisation to the
level of the factory cabs (-0.1 dBFS). The pedal does not give a slot's data back, so what is written is noted
in %APPDATA%\\MatriboxStudio\\ir_slots.json (the Studio's IRs tab reads the same file).
"""
import argparse, base64, cmath, datetime, json, math, os, struct, sys, zlib


RATE = 44100
SAMPLES = 1024                      # 16 partitions of 64 (CabIrInit), 23.2 ms
DATA_SIZE = SAMPLES * 4
SLOTS, NAME_MAX = 20, 16            # the pedal keeps 17 bytes with the terminator (FUN_000265b4)
FULL = 1 << 23
FACTORY_PEAK = 8287945              # peak of the factory cabs (-0.105 dBFS)
FADE = 64                           # samples of half-cosine fade when a longer IR is cut

CMD_IR_LIST, CMD_IR_DATA, CMD_IR_RENAME, CMD_IR_ACK = 0x14000000, 0x14000001, 0x14000002, 0x14000003

FORMATS = {1: 'PCM', 3: 'float'}


# ------------------------------------------------------------------------------------------------ WAV
def read_wav(path):
    """-> dict(rate, channels, bits, fmt, frames, samples = first channel as floats in -1..1)"""
    with open(path, 'rb') as f:
        data = f.read()
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('não é um arquivo WAV (RIFF/WAVE)')
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from('<I', data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            fmt = body
        elif cid == b'data':
            raw = body
            if fmt is not None:
                break
        pos += 8 + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise ValueError('WAV sem o bloco fmt')
    if raw is None:
        raise ValueError('WAV sem o bloco data')
    tag, ch, rate, _bps, align, bits = struct.unpack_from('<HHIIHH', fmt)
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack_from('<H', fmt, 24)[0]          # WAVE_FORMAT_EXTENSIBLE: the sub-format GUID
    if tag not in FORMATS:
        raise ValueError('formato %#x não suportado (só PCM e float)' % tag)
    if ch < 1 or rate < 8000:
        raise ValueError('cabeçalho inválido (%d canais, %d Hz)' % (ch, rate))
    width = bits // 8
    if tag == 1 and width not in (1, 2, 3, 4) or tag == 3 and width not in (4, 8):
        raise ValueError('%d bits %s não suportado' % (bits, FORMATS[tag]))
    align = align or width * ch
    frames = len(raw) // align
    out = []
    for k in range(frames):
        o = k * align
        if tag == 3:
            v = struct.unpack_from('<f' if width == 4 else '<d', raw, o)[0]
        elif width == 1:
            v = (raw[o] - 128) / 128
        elif width == 2:
            v = struct.unpack_from('<h', raw, o)[0] / 32768
        elif width == 3:
            v = int.from_bytes(raw[o:o + 3], 'little', signed=True) / FULL
        else:
            v = struct.unpack_from('<i', raw, o)[0] / 2147483648
        out.append(v if math.isfinite(v) else 0.0)
    return dict(rate=rate, channels=ch, bits=bits, fmt=FORMATS[tag], frames=frames, samples=out)


def resample(x, src, dst=RATE, n_out=None):
    """windowed-sinc (Blackman, 32 zero crossings) resampling of x from src to dst Hz"""
    if src == dst:
        return list(x[:n_out] if n_out else x)
    ratio = src / dst
    fc = min(1.0, dst / src) * 0.97                         # cut a bit below the lower Nyquist
    half = 32 / fc
    total = int(len(x) / ratio)
    n_out = min(total, n_out) if n_out else total
    out = []
    for n in range(n_out):
        t = n * ratio
        lo, hi = max(0, int(math.ceil(t - half))), min(len(x) - 1, int(t + half))
        acc = 0.0
        for k in range(lo, hi + 1):
            d = k - t
            u = d / half
            if abs(u) >= 1:
                continue
            w = 0.42 + 0.5 * math.cos(math.pi * u) + 0.08 * math.cos(2 * math.pi * u)
            s = fc if d == 0 else math.sin(math.pi * fc * d) / (math.pi * d)
            acc += x[k] * s * w
        out.append(acc)
    return out


def describe(path):
    """what the Studio shows about a .wav: format, length, peak, how much of it the 1024 pedal samples keep"""
    w = read_wav(path)
    x = w['samples']
    peak = max((abs(v) for v in x), default=0.0)
    d = dict(path=path, file=os.path.basename(path), name=os.path.splitext(os.path.basename(path))[0],
             rate=w['rate'], channels=w['channels'], bits=w['bits'], fmt=w['fmt'], frames=w['frames'],
             ms=w['frames'] * 1000.0 / w['rate'], peak_db=20 * math.log10(peak) if peak > 0 else None)
    total = sum(v * v for v in x)
    keep = int(round(SAMPLES * w['rate'] / RATE))
    d['kept'] = sum(v * v for v in x[:keep]) / total if total > 0 else 0.0
    d['ok'] = peak > 0
    d['reason'] = 'IR válido' if peak > 0 else 'o arquivo só tem silêncio'
    d['native'] = w['rate'] == RATE and w['bits'] == 24 and w['fmt'] == 'PCM'
    return d


def convert(path, normalize=False):
    """-> (1024 floats at 44.1 kHz, info dict)"""
    w = read_wav(path)
    x = resample(w['samples'], w['rate'], RATE, SAMPLES + 1)
    cut = len(x) > SAMPLES
    x = x[:SAMPLES]
    if cut:
        for k in range(FADE):
            x[SAMPLES - FADE + k] *= 0.5 + 0.5 * math.cos(math.pi * (k + 1) / FADE)
    x += [0.0] * (SAMPLES - len(x))
    peak = max(abs(v) for v in x)
    if peak <= 0:
        raise ValueError('o IR só tem silêncio')
    gain = FACTORY_PEAK / FULL / peak if normalize else 1.0
    x = [v * gain for v in x]
    clip = sum(1 for v in x if abs(v) >= 1.0)
    return x, dict(rate=w['rate'], resampled=w['rate'] != RATE, cut=cut, normalized=normalize,
                   gain_db=20 * math.log10(gain), clipped=clip)


def pack(samples):
    """1024 floats -> the slot's 4096 bytes (int32, 24-bit values)"""
    return struct.pack('<%di' % SAMPLES, *(max(-FULL, min(FULL - 1, int(round(v * FULL)))) for v in samples))


def unpack(data):
    return [v / FULL for v in struct.unpack('<%di' % SAMPLES, data[:DATA_SIZE])]


def pack_file(path, normalize=False):
    x, info = convert(path, normalize)
    data = pack(x)
    info['bytes'] = len(data)
    return data, info


# ------------------------------------------------------------------------------------------------ response
def _fft(a):
    """in-place iterative radix-2 FFT of a list of complex"""
    n = len(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        w0 = cmath.exp(-2j * math.pi / size)
        for s in range(0, n, size):
            w = 1
            for k in range(s, s + size // 2):
                u, v = a[k], a[k + size // 2] * w
                a[k], a[k + size // 2] = u + v, u - v
                w *= w0
        size <<= 1
    return a


def response(samples, points=160, f_lo=20.0, f_hi=20000.0, smooth=1 / 6, n_fft=4096):
    """magnitude response in dB at `points` log-spaced frequencies, smoothed over `smooth` octave"""
    a = _fft([complex(v) for v in samples[:n_fft]] + [0j] * (n_fft - min(len(samples), n_fft)))
    pw = [abs(a[k]) ** 2 for k in range(n_fft // 2 + 1)]
    cum = [0.0]
    for v in pw:
        cum.append(cum[-1] + v)
    df = RATE / n_fft
    out = []
    for i in range(points):
        f = f_lo * (f_hi / f_lo) ** (i / (points - 1))
        k1 = max(0, int(f * 2 ** (-smooth / 2) / df))
        k2 = min(len(pw) - 1, max(k1, int(math.ceil(f * 2 ** (smooth / 2) / df))))
        m = (cum[k2 + 1] - cum[k1]) / (k2 - k1 + 1)
        out.append((f, 10 * math.log10(m) if m > 1e-20 else -200.0))
    return out


# ------------------------------------------------------------------------------------------------ log
LOG_PATH = os.path.join(os.environ.get('APPDATA', os.path.dirname(os.path.abspath(__file__))), 'MatriboxStudio',
                        'ir_slots.json')


def load_log(path=LOG_PATH):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_log(log, path=LOG_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(log, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return log


def record(slot, name, wav, info, data, path=LOG_PATH):
    """note that User IR slot `slot` (0-based) now holds `wav` under `name` (with the data, to draw it later)"""
    log = load_log(path)
    log[str(slot)] = dict(name=name, file=os.path.basename(wav), path=os.path.abspath(wav),
                          date=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), rate=info['rate'],
                          normalized=bool(info['normalized']), crc32='%08x' % zlib.crc32(data),
                          data=base64.b64encode(data).decode('ascii'))
    return save_log(log, path)


def renamed(slot, old, new, path=LOG_PATH):
    """a slot written by us keeps its log entry when renamed (the entry is matched by name)"""
    log = load_log(path)
    e = log.get(str(slot))
    if e and e.get('name') == old:
        e['name'] = new
        save_log(log, path)
    return log


def log_samples(e):
    try:
        return unpack(base64.b64decode(e['data']))
    except Exception:
        return None


def default_name(path):
    return os.path.splitext(os.path.basename(path))[0].encode('latin1', 'replace').decode('latin1')[:NAME_MAX]


# ------------------------------------------------------------------------------------------------ CLI
def main():
    from matribox.usb import qmepreset as Q
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('info'); p.add_argument('wav')
    p = sub.add_parser('pack'); p.add_argument('wav'); p.add_argument('out'); p.add_argument('--normalize', action='store_true')
    p = sub.add_parser('list'); p.add_argument('--port')
    p = sub.add_parser('send'); p.add_argument('wav'); p.add_argument('--slot', type=int, required=True)
    p.add_argument('--name'); p.add_argument('--normalize', action='store_true'); p.add_argument('--port')
    p = sub.add_parser('rename'); p.add_argument('slot', type=int); p.add_argument('name'); p.add_argument('--port')
    a = ap.parse_args()
    if a.cmd == 'info':
        d = describe(a.wav)
        print('%s: %s %d-bit, %d Hz, %d canais, %d amostras (%.1f ms), pico %s' % (
            d['file'], d['fmt'], d['bits'], d['rate'], d['channels'], d['frames'], d['ms'],
            '%.2f dBFS' % d['peak_db'] if d['peak_db'] is not None else '-'))
        print('no pedal: 1024 amostras a 44,1 kHz (23,2 ms), %.1f%% da energia' % (100 * d['kept']))
        return
    if a.cmd == 'pack':
        data, info = pack_file(a.wav, a.normalize)
        open(a.out, 'wb').write(data)
        print('%s: %d bytes%s' % (a.out, len(data), ', reamostrado de %d Hz' % info['rate'] if info['resampled'] else ''))
        return
    pedal = Q.Pedal(a.port)
    try:
        if a.cmd == 'list':
            for i, n in enumerate(pedal.ir_names()):
                print('IR %2d  %s' % (i + 1, n))
        elif a.cmd == 'send':
            slot = a.slot - 1
            name = (a.name or default_name(a.wav))[:NAME_MAX]
            data, info = pack_file(a.wav, a.normalize)
            pedal.write_ir(slot, name, data)
            record(slot, name, a.wav, info, data)
            print('User IR %d: "%s" gravado' % (a.slot, name))
        elif a.cmd == 'rename':
            old = pedal.ir_names()[a.slot - 1]
            pedal.rename_ir(a.slot - 1, a.name)
            renamed(a.slot - 1, old, a.name[:NAME_MAX])
            print('User IR %d: "%s" -> "%s"' % (a.slot, old, a.name[:NAME_MAX]))
    finally:
        pedal.close()


if __name__ == '__main__':
    main()
