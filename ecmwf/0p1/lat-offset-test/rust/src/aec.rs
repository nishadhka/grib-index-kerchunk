//! Minimal libaec binding, loaded at run time from the same libaec.so the
//! Python tests use (imagecodecs wheel), so both sides run the same decoder.

use libloading::Library;
use std::ffi::c_void;

#[repr(C)]
struct AecStream {
    next_in: *const u8,
    avail_in: usize,
    total_in: usize,
    next_out: *mut u8,
    avail_out: usize,
    total_out: usize,
    bits_per_sample: u32,
    block_size: u32,
    rsi: u32,
    flags: u32,
    state: *mut c_void,
}

type FStrm = unsafe extern "C" fn(*mut AecStream) -> i32;
type FDecode = unsafe extern "C" fn(*mut AecStream, i32) -> i32;
type FSeek = unsafe extern "C" fn(*mut AecStream, usize) -> i32;
type FCount = unsafe extern "C" fn(*mut AecStream, *mut usize) -> i32;
type FGet = unsafe extern "C" fn(*mut AecStream, *mut usize, usize) -> i32;

pub struct Aec {
    _lib: Library,
    init: FStrm,
    decode: FDecode,
    end: FStrm,
    seek: FSeek,
    enable_offsets: FStrm,
    count_offsets: FCount,
    get_offsets: FGet,
}

/// CCSDS parameters of one message (GRIB2 template 5.42).
#[derive(Clone, Copy)]
pub struct Params {
    pub bps: u32,
    pub block_size: u32,
    pub rsi: u32,
    pub flags: u32,
}

impl Params {
    pub fn bytes_per_sample(&self) -> usize {
        if self.bps <= 16 { 2 } else { 4 }
    }
}

/// Samples decoded from an exact bit, plus (optionally) the RSI start of
/// every RSI begun, as absolute bit positions in the input buffer.
pub struct Decoded {
    pub x: Vec<u32>,
    pub offsets: Vec<i64>,
}

impl Aec {
    pub fn load(path: &str) -> Result<Self, libloading::Error> {
        unsafe {
            let lib = Library::new(path)?;
            Ok(Aec {
                init: *lib.get::<FStrm>(b"aec_decode_init\0")?,
                decode: *lib.get::<FDecode>(b"aec_decode\0")?,
                end: *lib.get::<FStrm>(b"aec_decode_end\0")?,
                seek: *lib.get::<FSeek>(b"aec_buffer_seek\0")?,
                enable_offsets: *lib.get::<FStrm>(b"aec_decode_enable_offsets\0")?,
                count_offsets: *lib.get::<FCount>(b"aec_decode_count_offsets\0")?,
                get_offsets: *lib.get::<FGet>(b"aec_decode_get_offsets\0")?,
                _lib: lib,
            })
        }
    }

    /// Decode `nsamp` samples starting at an exact bit of `buf`. With
    /// `to_end`, decode as many as there are, stopping at the end of `buf`.
    /// None on a decoder error (or, without `to_end`, a short output).
    pub fn decode_at(&self, buf: &[u8], bit: i64, nsamp: usize, p: Params,
                     want_offsets: bool, to_end: bool) -> Option<Decoded> {
        self.decode_impl(buf, bit, nsamp, p, want_offsets, if to_end { 1 } else { 0 }, to_end)
    }

    /// Decode as far as `buf` goes (it may end mid-block, as a fetched window
    /// does): no flush, and a short output is not an error.
    pub fn decode_partial(&self, buf: &[u8], bit: i64, nsamp: usize, p: Params) -> Option<Decoded> {
        self.decode_impl(buf, bit, nsamp, p, true, 0, true)
    }

    fn decode_impl(&self, buf: &[u8], bit: i64, nsamp: usize, p: Params, want_offsets: bool,
                   flush: i32, short_ok: bool) -> Option<Decoded> {
        if bit < 0 || (bit / 8) as usize >= buf.len() {
            return None;
        }
        let (b0, sh) = ((bit / 8) as usize, (bit % 8) as usize);
        let bpsamp = p.bytes_per_sample();
        let mut out = vec![0u8; nsamp * bpsamp];
        let mut s = AecStream {
            next_in: buf[b0..].as_ptr(), avail_in: buf.len() - b0, total_in: 0,
            next_out: out.as_mut_ptr(), avail_out: out.len(), total_out: 0,
            bits_per_sample: p.bps, block_size: p.block_size, rsi: p.rsi, flags: p.flags,
            state: std::ptr::null_mut(),
        };
        unsafe {
            if (self.init)(&mut s) != 0 {
                return None;
            }
            if want_offsets {
                (self.enable_offsets)(&mut s);
            }
            if sh != 0 && (self.seek)(&mut s, sh) != 0 {
                (self.end)(&mut s);
                return None;
            }
            let rc = (self.decode)(&mut s, flush);
            let ok = rc == 0 && (short_ok || s.avail_out == 0);
            let got = (out.len() - s.avail_out) / bpsamp;
            let mut offsets = Vec::new();
            if ok && want_offsets {
                let mut c = 0usize;
                (self.count_offsets)(&mut s, &mut c);
                let mut o = vec![0usize; c];
                (self.get_offsets)(&mut s, o.as_mut_ptr(), c);
                // libaec counts from buf[b0..] and reports 8 bits short after a
                // non-zero bit seek (v2 finding); entry 0 is its seeded 0.
                let adj = (b0 * 8 + if sh != 0 { 8 } else { 0 }) as i64;
                offsets = o.iter().map(|&v| v as i64 + adj).collect();
                if let Some(f) = offsets.first_mut() {
                    *f = bit;
                }
            }
            (self.end)(&mut s);
            if !ok {
                return None;
            }
            let x = if bpsamp == 2 {
                out[..got * 2].chunks_exact(2).map(|c| u16::from_be_bytes([c[0], c[1]]) as u32).collect()
            } else {
                out[..got * 4].chunks_exact(4).map(|c| u32::from_be_bytes([c[0], c[1], c[2], c[3]])).collect()
            };
            Some(Decoded { x, offsets })
        }
    }
}
