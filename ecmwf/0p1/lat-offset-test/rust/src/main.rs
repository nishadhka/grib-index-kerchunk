//! Benchmark the Rust search against the Python v5 search on the same cached
//! bytes (rust/dump_cases.py, rust/truth_cases.py).
//!
//!     AECSYNC_LIBAEC=/path/to/libaec.so cargo run --release -- <cases dir> <mode> [repeats]
//!
//! modes:
//!   v5-libaec  the v5 search, candidate test through libaec (as the Python)
//!   v5-fast    the v5 search, candidate test through fastdec.rs
//!              (both v5 modes must give the Python's answers exactly)
//!   converge   two chains from different starts; trust from their first
//!              shared offset (then as chain)
//!   chain      stop at the first verified start and use its RSI chain; then
//!              the two read policies (count to the end; tp counted, others
//!              value match + gate), checked against the true RSI offsets

mod aec;
mod fastdec;
mod sync;

use aec::{Aec, Params};
use serde::Deserialize;
use std::time::Instant;
use sync::{candidates, chain_sync, converge_sync, count_to_end, score_all, Ctx, Dec, Unpack};

const SIGMA: f64 = 0.01;
const LAMBDA: f64 = 0.25;
const GATE: f64 = 0.45;

#[derive(Deserialize)]
struct Py {
    starts: Vec<i64>,
    kpred: i64,
    n_cands: usize,
    best: Option<(i64, i64, f64)>,
    count_start: i64,
    count: i64,
    t_sync: f64,
    t_score: f64,
    t_count: f64,
}

#[derive(Deserialize)]
struct Case {
    field: String,
    step: u32,
    win_len: usize,
    tail_len: usize,
    max_bits: i64,
    bps: u32,
    block_size: u32,
    rsi: u32,
    flags: u32,
    ref_value: f64,
    bin_scale: i32,
    dec_scale: i32,
    nrsi: usize,
    w0: i64,
    pred_bits: Vec<i64>,
    kspan: i64,
    ref_k0: usize,
    ref_std: f64,
    py: Py,
}

fn best_of<T>(n: usize, mut f: impl FnMut() -> T) -> (T, f64) {
    let mut best = f64::MAX;
    let mut out = None;
    for _ in 0..n {
        let t = Instant::now();
        let r = f();
        best = best.min(t.elapsed().as_secs_f64());
        out = Some(r);
    }
    (out.unwrap(), best)
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dir = &args[1];
    let mode = args.get(2).map(|s| s.as_str()).unwrap_or("v5-fast");
    let reps: usize = args.get(3).map(|s| s.parse().unwrap()).unwrap_or(3);
    let lib = std::env::var("AECSYNC_LIBAEC").expect("set AECSYNC_LIBAEC to libaec.so");
    let aec = Aec::load(&lib).expect("load libaec");
    let cases: Vec<Case> =
        serde_json::from_reader(std::fs::File::open(format!("{dir}/manifest.json")).unwrap()).unwrap();
    let truth: Vec<Vec<i64>> = std::fs::File::open(format!("{dir}/truth.json"))
        .map(|f| serde_json::from_reader(f).unwrap()).unwrap_or_default();
    let dec = if mode == "v5-libaec" { Dec::Libaec } else { Dec::Fast };
    let mut cx = Ctx::new(&aec, dec);
    println!("mode {mode}, best of {reps}, single thread");

    let (mut py_tot, mut rs_tot, mut good, mut good2) = (0.0, 0.0, 0usize, 0usize);
    let (mut n_matched, mut late) = (0usize, 0usize);
    for (i, c) in cases.iter().enumerate() {
        let tail = std::fs::read(format!("{dir}/case_{i:03}.bin")).unwrap();
        let buf = &tail[..c.win_len];
        let reff: Vec<f64> = std::fs::read(format!("{dir}/ref_{i:03}.f64")).unwrap()
            .chunks_exact(8).map(|b| f64::from_le_bytes(b.try_into().unwrap())).collect();
        let p = Params { bps: c.bps, block_size: c.block_size, rsi: c.rsi, flags: c.flags };
        let u = Unpack { ref_value: c.ref_value, bin_scale: c.bin_scale, dec_scale: c.dec_scale };
        let py = c.py.t_sync + c.py.t_score + c.py.t_count;
        py_tot += py;

        if mode.starts_with("v5") {
            let (starts, t_sync) = best_of(reps, || candidates(&mut cx, buf, p, c.max_bits));
            let (cands, t_score) = best_of(reps, || {
                score_all(&aec, buf, &starts, p, &u, c.py.kpred, c.kspan, &reff, c.ref_k0, c.ref_std)
            });
            let best = cands.iter().copied().fold(None, |b: Option<(i64, i64, f64)>, x| match b {
                Some(b) if b.2 <= x.2 => Some(b),
                _ => Some(x),
            });
            let st = best.map(|b| b.0).unwrap_or(starts.first().copied().unwrap_or(0));
            let (count, t_count) = best_of(reps, || count_to_end(&aec, &tail, st, c.nrsi, p));
            let same_best = match (best, c.py.best) {
                (Some(a), Some(b)) => a.0 == b.0 && a.1 == b.1 && (a.2 - b.2).abs() <= 1e-9 * b.2.abs().max(1.0),
                (None, None) => true,
                _ => false,
            };
            // the Python counts were taken on tails cut d0 bytes short (fixed since),
            // so the count is checked against the true offsets instead
            let kc = c.nrsi as i64 - count.map(|v| v as i64).unwrap_or(c.nrsi as i64 + 1);
            let count_ok = truth.get(i).map(|tr| kc >= 0 && (kc as usize) < tr.len() && tr[kc as usize] == st)
                .unwrap_or(false);
            let _ = c.py.count;
            let same = starts == c.py.starts && cands.len() == c.py.n_cands && same_best
                && st == c.py.count_start && count_ok;
            good += same as usize;
            let rs = t_sync + t_score + t_count;
            rs_tot += rs;
            println!("{i:3} {:5} {:4} | py {:6.2}s | rs sync {:8.2}ms score {:5.1}ms count {:4.1}ms | {:6.0}x | {}",
                     c.field, c.step, py, 1e3 * t_sync, 1e3 * t_score, 1e3 * t_count, py / rs,
                     if same { "same as Python, count exact" } else { "DIFFERENT" });
            continue;
        }

        // ---- chain mode: new search, judged against the true offsets ----
        let tr = &truth[i];
        let (ni, nj) = if c.nrsi == 254 { (1440usize, 721usize) } else { (3600, 1801) };
        let dlat = 180.0 / (nj - 1) as f64;
        let k0 = ((90.0f64 - 40.0) / dlat).round() as usize * ni / sync::RS; // band 40N..40S
        let (chain, t_sync) = best_of(reps, || if mode == "converge" {
            converge_sync(&mut cx, buf, p, c.max_bits, c.nrsi)
        } else {
            chain_sync(&mut cx, buf, p, c.max_bits, c.nrsi)
        });
        if chain.is_empty() {
            println!("{i:3} {:5} {:4} | no sync -> full read", c.field, c.step);
            continue;
        }
        let st = chain[0];
        if std::env::var("AECSYNC_DEBUG").is_ok() {
            let hit: Vec<String> = chain.iter().take(12).map(|o| match tr.iter().position(|t| t == o) {
                Some(k) => format!("{k}"), None => "x".into() }).collect();
            eprintln!("case {i}: chain[0..12] true RSI index: {}", hit.join(" "));
        }
        // count policy: name the RSI by counting to the end of the packed data
        let (cnt, t_count) = best_of(reps, || count_to_end(&aec, &tail, st, c.nrsi, p));
        let kc = c.nrsi as i64 - cnt.unwrap_or(0) as i64;
        let ok_count = kc >= 0 && (kc as usize) < tr.len() && tr[kc as usize] == st;
        // value match on the chain start (v5 pick rule), gated; tp always counted
        let at = c.w0 * 8 + st;
        let kpred = (0..c.pred_bits.len()).min_by_key(|&k| (c.pred_bits[k] - at).abs()).unwrap() as i64;
        let (cands, t_score) = best_of(reps, || {
            score_all(&aec, buf, &[st], p, &u, kpred, c.kspan, &reff, c.ref_k0, c.ref_std)
        });
        let l_bits = ((c.w0 as usize + c.tail_len + 4) * 8) as f64;
        let best_sc = cands.iter().map(|x| x.2).fold(f64::MAX, f64::min).max(1e-12);
        let cost = |x: &(i64, i64, f64)| {
            x.2 / best_sc + LAMBDA * ((c.pred_bits[x.1 as usize] - (c.w0 * 8 + x.0)) as f64 / (SIGMA * l_bits)).powi(2)
        };
        let pick = cands.iter().copied().min_by(|a, b| cost(a).partial_cmp(&cost(b)).unwrap());
        let counted = c.field == "tp" || pick.map(|x| x.2 > GATE).unwrap_or(true);
        let kv = if counted { kc } else { pick.unwrap().1 };
        let ok_policy = kv >= 0 && (kv as usize) < tr.len() && tr[kv as usize] == st;
        good += ok_count as usize;
        good2 += ok_policy as usize;
        n_matched += !counted as usize;
        late += (kc > k0 as i64) as usize;
        let rs = t_sync + t_count + if counted { 0.0 } else { t_score };
        rs_tot += rs;
        println!("{i:3} {:5} {:4} | py {:6.2}s | rs sync {:7.2}ms count {:4.1}ms score {:4.1}ms | {:6.0}x | count {} | {} {}{}",
                 c.field, c.step, py, 1e3 * t_sync, 1e3 * t_count, 1e3 * t_score, py / rs,
                 if ok_count { "exact" } else { "WRONG" }, if counted { "counted" } else { "matched" },
                 if ok_policy { "exact" } else { "WRONG" },
                 if kc > k0 as i64 { "  (chain starts after the band: head GET)" } else { "" });
    }
    let n = cases.len();
    if mode.starts_with("v5") {
        println!("\n{n} messages: Python {:.2} s/msg, Rust {:.1} ms/msg, speed-up {:.0}x; same answers as Python {good}/{n}",
                 py_tot / n as f64, 1e3 * rs_tot / n as f64, py_tot / rs_tot);
    } else {
        println!("\n{n} messages: Python v5 {:.2} s/msg, Rust {mode} {:.1} ms/msg, speed-up {:.0}x; \
                  count policy exact {good}/{n}; tp policy exact {good2}/{n} ({n_matched} value-matched); \
                  chain starts after the band in {late}",
                 py_tot / n as f64, 1e3 * rs_tot / n as f64, py_tot / rs_tot);
    }
}
