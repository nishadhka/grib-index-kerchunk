//! Rust port of the v5 search (../v5/sync_band5.py): bit-sync to an exact RSI
//! start inside a window, value-match against a template, count RSIs to the
//! end of the packed data. Same rules and thresholds as the Python.
//!
//! Two decoders for the per-bit candidate test: `Dec::Libaec` (one libaec
//! init per try, as the Python does) and `Dec::Fast` (fastdec.rs, no
//! allocation). Two searches: `candidates` (v5: every distinct start over ~2
//! RSIs of bits) and `chain_sync` (stop at the first verified start and take
//! the starts 2+ RSIs along its chain, which are exact).

use crate::aec::{Aec, Params};
use crate::fastdec;

pub const RS: usize = 4096; // samples per RSI (block_size 32 x rsi 128)
pub const NR: usize = 12; // RSIs per alignment strip
pub const SMOOTH: f64 = 0.02; // max mean |adjacent diff| / code range

#[derive(Clone, Copy, PartialEq)]
pub enum Dec {
    Libaec,
    Fast,
}

pub struct Ctx<'a> {
    pub aec: &'a Aec,
    pub dec: Dec,
    out: Vec<u64>,
    offs: Vec<u64>,
}

impl<'a> Ctx<'a> {
    pub fn new(aec: &'a Aec, dec: Dec) -> Self {
        Ctx { aec, dec, out: Vec::with_capacity(3 * RS), offs: Vec::with_capacity(8) }
    }
}

fn rough<T: Copy + Into<u64>>(x: &[T], bps: u32) -> f64 {
    let s: u64 = x.windows(2).map(|w| (w[1].into() as i64 - w[0].into() as i64).unsigned_abs()).sum();
    s as f64 / (x.len() - 1) as f64 / (1u64 << bps) as f64
}

/// Start of a smooth, self-consistent RSI chain near `bit`, and the length
/// of that RSI in bits.
pub fn candidate(cx: &mut Ctx, buf: &[u8], bit: i64, p: Params) -> Option<(i64, i64)> {
    if cx.dec == Dec::Fast {
        return candidate_fast(cx, buf, bit, p);
    }
    let aec = cx.aec;
    let x = aec.decode_at(buf, bit, 64, p, false, false)?.x; // cheap prefilter: 2 blocks
    if rough(&x, p.bps) > SMOOTH || x.iter().any(|&v| (v as u64) >= (1u64 << p.bps)) {
        return None;
    }
    let r = aec.decode_at(buf, bit, 3 * RS, p, true, false)?;
    if r.offsets.len() < 3 {
        return None;
    }
    if !(0..3).all(|i| rough(&r.x[i * RS..(i + 1) * RS], p.bps) < SMOOTH) {
        return None;
    }
    // a start a few bits early can still decode smoothly: hop to the second
    // start it reports and keep it only if that chain agrees on the third
    let nb = r.offsets[1];
    let r2 = aec.decode_at(buf, nb, 3 * RS, p, true, false)?;
    if r2.offsets.len() < 2 || r2.offsets[1] != r.offsets[2] {
        return None;
    }
    Some((nb, r.offsets[2] - r.offsets[1]))
}

/// The same test with fastdec: no libaec call, no allocation.
fn candidate_fast(cx: &mut Ctx, buf: &[u8], bit: i64, p: Params) -> Option<(i64, i64)> {
    let (bs, rsi) = (p.block_size as usize, p.rsi as usize);
    if bit < 0 || !fastdec::decode(buf, bit as u64, p.bps, bs, rsi, 64, &mut cx.out, &mut cx.offs) {
        return None;
    }
    if rough(&cx.out, p.bps) > SMOOTH || cx.out.iter().any(|&v| v >= (1u64 << p.bps)) {
        return None;
    }
    // 3 RSIs, one at a time, each stopping as soon as it is certainly rough
    // (the same pass/fail as decoding all 3 and testing each)
    let mut starts = [bit as u64; 4];
    for j in 0..3 {
        starts[j + 1] = fastdec::smooth_rsi(buf, starts[j], p.bps, bs, rsi, SMOOTH, &mut cx.offs)?;
    }
    cx.offs.clear();
    cx.offs.extend_from_slice(&starts[..3]);
    let (o1, o2) = (cx.offs[1], cx.offs[2]);
    if !fastdec::decode(buf, o1, p.bps, bs, rsi, 3 * RS, &mut cx.out, &mut cx.offs) || cx.offs.len() < 2
        || cx.offs[1] != o2 {
        return None;
    }
    Some((o1 as i64, (o2 - o1) as i64))
}

/// v5 search: every distinct candidate start from the first one found to ~1 RSI later.
pub fn candidates(cx: &mut Ctx, buf: &[u8], p: Params, max_bits: i64) -> Vec<i64> {
    let mut first = None;
    for bit in 0..max_bits {
        if let Some(c) = candidate(cx, buf, bit, p) {
            first = Some((bit, c));
            break;
        }
    }
    let Some((bit, c)) = first else { return vec![] };
    let mut found = vec![c.0];
    for b2 in bit + 1..bit + 2 * c.1 + 1 {
        if let Some(c2) = candidate(cx, buf, b2, p) {
            if !found.contains(&c2.0) {
                found.push(c2.0);
            }
        }
    }
    found.sort_unstable();
    found
}

/// Chain search: the first verified start, then its RSI chain decoded to the
/// end of `buf`. Returns the chain from 2 RSIs past the start on (exact even
/// when the first start is a false one that fell into step).
pub fn chain_sync(cx: &mut Ctx, buf: &[u8], p: Params, max_bits: i64, nrsi: usize) -> Vec<i64> {
    for bit in 0..max_bits {
        if let Some((nb, _)) = candidate(cx, buf, bit, p) {
            // a false start (tp zero runs) can pass the 3-RSI test and break
            // down later: then keep scanning
            if let Some(r) = cx.aec.decode_partial(buf, nb, nrsi * RS, p) {
                if r.offsets.len() > 2 {
                    return r.offsets[2..].to_vec();
                }
            }
        }
    }
    vec![]
}

/// Cheaper candidate test for the chain search: the 64-sample prefilter, then
/// up to 3 RSIs decoded one at a time with an early exit on the first rough
/// one. No hop check: on a deterministic decoder it cannot fail.
fn quick_candidate(cx: &mut Ctx, buf: &[u8], bit: i64, p: Params) -> bool {
    let (bs, rsi) = (p.block_size as usize, p.rsi as usize);
    if bit < 0 || !fastdec::decode(buf, bit as u64, p.bps, bs, rsi, 64, &mut cx.out, &mut cx.offs) {
        return false;
    }
    if rough(&cx.out, p.bps) > SMOOTH || cx.out.iter().any(|&v| v >= (1u64 << p.bps)) {
        return false;
    }
    let mut at = bit as u64;
    for _ in 0..3 {
        if !fastdec::decode(buf, at, p.bps, bs, rsi, RS + 1, &mut cx.out, &mut cx.offs) || cx.offs.len() < 2 {
            return false;
        }
        if rough(&cx.out[..RS], p.bps) >= SMOOTH {
            return false;
        }
        at = cx.offs[1];
    }
    true
}

/// The first chain that decodes through the window, scanning from `from`.
fn first_chain(cx: &mut Ctx, buf: &[u8], p: Params, from: i64, to: i64, nrsi: usize) -> Option<(i64, Vec<i64>)> {
    for bit in from..to {
        if quick_candidate(cx, buf, bit, p) {
            if let Some(r) = cx.aec.decode_partial(buf, bit, nrsi * RS, p) {
                if r.offsets.len() > 3 {
                    return Some((bit, r.offsets));
                }
            }
        }
    }
    None
}

/// Convergence search: chain A from the first candidate, chain B from the
/// first candidate after A's first RSI start; trust offsets from the first
/// one both chains share (two misaligned decodes fall into step on the true
/// RSI track, not on the same false one).
pub fn converge_sync(cx: &mut Ctx, buf: &[u8], p: Params, max_bits: i64, nrsi: usize) -> Vec<i64> {
    let Some((_, a)) = first_chain(cx, buf, p, 0, max_bits, nrsi) else { return vec![] };
    let mut from = a[0] + 1;
    while from < max_bits + 2 * (a[1] - a[0]) {
        let Some((bit, b)) = first_chain(cx, buf, p, from, max_bits + 2 * (a[1] - a[0]), nrsi) else { break };
        if let Some(i) = a.iter().position(|o| b.contains(o)) {
            if a[i] != b[0] {
                return a[i..].to_vec(); // B started elsewhere and fell onto A's track
            }
        }
        from = bit + 1; // B starts on A's track or never meets it: look further
    }
    vec![]
}

pub struct Unpack {
    pub ref_value: f64,
    pub bin_scale: i32,
    pub dec_scale: i32,
}

/// Every (start, k, value score) in kpred +- kspan. `reff` holds the template
/// values from RSI `ref_k0` on; `scale` is the template's standard deviation.
pub fn score_all(aec: &Aec, buf: &[u8], starts: &[i64], p: Params, u: &Unpack, kpred: i64,
                 kspan: i64, reff: &[f64], ref_k0: usize, scale: f64) -> Vec<(i64, i64, f64)> {
    let mut out = vec![];
    let (e, d) = (2f64.powi(u.bin_scale), 10f64.powi(u.dec_scale));
    for &st in starts {
        let Some(r) = aec.decode_at(buf, st, NR * RS, p, false, false) else { continue };
        let v: Vec<f64> = r.x.iter().map(|&x| (u.ref_value + x as f64 * e) / d).collect();
        for k in (kpred - kspan).max(0)..=kpred + kspan {
            let Some(a) = (k as usize).checked_sub(ref_k0).map(|i| i * RS) else { continue };
            if a + v.len() > reff.len() {
                break;
            }
            let s: f64 = v.iter().zip(&reff[a..a + v.len()]).map(|(x, y)| (x - y).abs()).sum();
            out.push((st, k, s / v.len() as f64 / scale));
        }
    }
    out
}

/// RSIs from an exact start to the end of the packed data (Section 8 excluded).
pub fn count_to_end(aec: &Aec, tail: &[u8], st: i64, nrsi: usize, p: Params) -> Option<usize> {
    aec.decode_at(tail, st, nrsi * RS, p, true, true).map(|r| r.offsets.len())
}
