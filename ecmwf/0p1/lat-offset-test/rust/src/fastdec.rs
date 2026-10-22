//! Allocation-free CCSDS (AEC) decoder from an arbitrary bit, for the search.
//! libaec costs an init + free per try; this reads the bits directly. It
//! follows libaec's decoder (decode.c): ID, reference sample, zero-block /
//! second-extension / split / uncompressed options, the zero-block "remainder
//! of segment" rule, RSI boundaries, and the inverse unit-delay preprocessor
//! for unsigned data. Only unsigned, preprocessed streams (GRIB2 template
//! 5.42 as ECMWF writes it: ccsds flags 14) are handled.

const ROS: u64 = 5; // "remainder of segment" zero-block code
const SE_MAX: u64 = 90; // largest second-extension code (12*13/2 + 12)

struct Bits<'a> {
    buf: &'a [u8],
    pos: u64, // absolute bit position
    end: u64, // bits in buf
}

impl<'a> Bits<'a> {
    /// The next 57+ bits at `pos`, MSB-aligned, zero-padded past the end.
    #[inline]
    fn peek(&self) -> u64 {
        let b = (self.pos >> 3) as usize;
        let w = if b + 8 <= self.buf.len() {
            u64::from_be_bytes(self.buf[b..b + 8].try_into().unwrap())
        } else {
            let mut w = 0u64;
            for i in 0..8 {
                w = (w << 8) | *self.buf.get(b + i).unwrap_or(&0) as u64;
            }
            w
        };
        w << (self.pos & 7)
    }
    #[inline]
    fn get(&mut self, n: u32) -> Option<u64> {
        if n == 0 {
            return Some(0);
        }
        if self.pos + n as u64 > self.end {
            return None;
        }
        let v = self.peek() >> (64 - n);
        self.pos += n as u64;
        Some(v)
    }
    /// Fundamental sequence: number of 0 bits before the next 1.
    #[inline]
    fn fs(&mut self) -> Option<u64> {
        let mut n = 0u64;
        loop {
            if self.pos >= self.end {
                return None;
            }
            let lz = self.peek().leading_zeros() as u64;
            if lz < 57 {
                if self.pos + lz + 1 > self.end {
                    return None;
                }
                self.pos += lz + 1;
                return Some(n + lz);
            }
            self.pos += 57;
            n += 57;
        }
    }
}

/// Decode `nsamp` samples from `bit`, treating it as an RSI start, as libaec
/// would with AEC_NO_FLUSH. `out` gets the samples (as libaec writes them:
/// 2-byte for bps <= 16), `offs` the bit position where each RSI begins.
/// Both are reused by the caller. False where libaec would fail.
pub fn decode(buf: &[u8], bit: u64, bps: u32, block_size: usize, rsi: usize, nsamp: usize,
              out: &mut Vec<u64>, offs: &mut Vec<u64>) -> bool {
    out.clear();
    run(buf, bit, bps, block_size, rsi, nsamp, offs, |v| {
        out.push(v);
        true
    })
}

/// Decode one RSI from `bit` and test it as the search does (mean |adjacent
/// difference| / 2^bps below `smooth`), stopping as soon as the running sum
/// makes the answer certain. Some(start of the next RSI) if it passes.
pub fn smooth_rsi(buf: &[u8], bit: u64, bps: u32, block_size: usize, rsi: usize, smooth: f64,
                  offs: &mut Vec<u64>) -> Option<u64> {
    let n = rsi * block_size;
    let scale = (n - 1) as f64 * (1u64 << bps) as f64;
    let (mut prev, mut sum, mut seen) = (0u64, 0u64, 0usize);
    let ok = run(buf, bit, bps, block_size, rsi, n + 1, offs, |v| {
        if seen > 0 && seen < n {
            sum += v.abs_diff(prev);
            if sum as f64 / scale >= smooth {
                return false; // already rough: the full RSI cannot pass
            }
        }
        prev = v;
        seen += 1;
        true
    });
    if ok && offs.len() >= 2 { Some(offs[1]) } else { None }
}

fn run(buf: &[u8], bit: u64, bps: u32, block_size: usize, rsi: usize, nsamp: usize,
       offs: &mut Vec<u64>, mut sink: impl FnMut(u64) -> bool) -> bool {
    offs.clear();
    if bps == 0 || bps > 32 {
        return false;
    }
    let id_len: u32 = if bps > 16 { 5 } else if bps > 8 { 4 } else if bps > 4 { 3 } else { 2 };
    let id_uncomp = (1u64 << id_len) - 1;
    let omask: u64 = if bps <= 16 { 0xFFFF } else { 0xFFFF_FFFF };
    let xmax: i64 = ((1u64 << bps) - 1) as i64;
    let med: i64 = xmax / 2 + 1;
    let mut r = Bits { buf, pos: bit, end: buf.len() as u64 * 8 };
    let mut d: Vec<u64> = Vec::with_capacity(block_size * 64);
    let mut done = 0usize;
    while done < nsamp {
        // one RSI: `rsi` blocks, the first carrying the reference sample
        offs.push(r.pos);
        let mut b = 0usize;
        let mut data = 0i64;
        let mut first = true;
        while b < rsi && done < nsamp {
            d.clear();
            let refb = b == 0;
            let Some(id) = r.get(id_len) else { return false };
            if refb && id != 0 {
                let Some(x) = r.get(bps) else { return false };
                d.push(x);
            }
            let mut nblocks = 1usize;
            if id == 0 {
                let Some(sel) = r.get(1) else { return false };
                if refb {
                    let Some(x) = r.get(bps) else { return false };
                    d.push(x);
                }
                if sel == 0 {
                    let Some(fs) = r.fs() else { return false };
                    let mut zb = fs + 1;
                    if zb == ROS {
                        zb = ((rsi - b) as u64).min(64 - (b as u64 % 64));
                    } else if zb > ROS {
                        zb -= 1;
                    }
                    if b + zb as usize > rsi {
                        return false; // libaec: zero run past the RSI
                    }
                    let n = zb as usize * block_size - refb as usize;
                    d.extend(std::iter::repeat(0).take(n));
                    nblocks = zb as usize;
                } else {
                    let mut i = refb as usize;
                    while i < block_size {
                        let Some(m) = r.fs() else { return false };
                        if m > SE_MAX {
                            return false;
                        }
                        let mut s = 0u64;
                        while (s + 1) * (s + 2) / 2 <= m {
                            s += 1;
                        }
                        let d1 = m - s * (s + 1) / 2;
                        if i & 1 == 0 {
                            d.push(s - d1);
                            i += 1;
                        }
                        d.push(d1);
                        i += 1;
                    }
                }
            } else if id == id_uncomp {
                for _ in refb as usize..block_size {
                    let Some(x) = r.get(bps) else { return false };
                    d.push(x);
                }
            } else {
                let k = (id - 1) as u32;
                for _ in refb as usize..block_size {
                    let Some(f) = r.fs() else { return false };
                    d.push(f << k);
                }
                if k > 0 {
                    for v in d.iter_mut().skip(refb as usize) {
                        let Some(l) = r.get(k) else { return false };
                        *v |= l;
                    }
                }
            }
            b += nblocks;
            // inverse preprocessor (libaec FLUSH_KIND, unsigned); data restarts
            // at each RSI's reference sample
            for &dv in &d {
                if done >= nsamp {
                    break;
                }
                let v = if first {
                    first = false;
                    data = dv as i64;
                    dv
                } else {
                    let dv = dv as i64;
                    let half = (dv >> 1) + (dv & 1);
                    let mask = if data & med != 0 { xmax } else { 0 };
                    if half <= (mask ^ data) {
                        data += if dv & 1 == 0 { dv >> 1 } else { -(dv >> 1) - 1 };
                    } else {
                        data = mask ^ dv;
                    }
                    data as u64
                };
                done += 1;
                if !sink(v & omask) {
                    return false;
                }
            }
        }
    }
    true
}
