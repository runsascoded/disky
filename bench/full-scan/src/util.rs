//! Small shared pieces: hashing, Arrow column access, errors.

use std::hash::{BuildHasherDefault, Hasher};

use arrow_array::cast::AsArray;
use arrow_array::types::{Int16Type, Int32Type, Int64Type, Int8Type, UInt16Type, UInt32Type, UInt64Type, UInt8Type};
use arrow_array::{Array, ArrayRef};
use arrow_schema::DataType;

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;

pub fn err<T>(msg: impl Into<String>) -> Result<T> {
    Err(msg.into().into())
}

/// A 128-bit hash: a path's or a name's identity during the load (collisions are ~2^-64 at 10^9 keys).
pub fn h128(b: &[u8]) -> u128 {
    xxhash_rust::xxh3::xxh3_128(b)
}

/// A `Hasher` for keys that already are hashes: their low 64 bits.
#[derive(Default)]
pub struct IdHasher(u64);

impl Hasher for IdHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes.iter().take(8) {
            self.0 = (self.0 << 8) | b as u64;
        }
    }
    fn write_u128(&mut self, i: u128) {
        self.0 = i as u64;
    }
    fn write_u64(&mut self, i: u64) {
        self.0 = i;
    }
}

pub type IdBuild = BuildHasherDefault<IdHasher>;
pub type IdMap<K, V> = std::collections::HashMap<K, V, IdBuild>;

/// An integer column of any width as `i64`s.
pub fn ints(a: &ArrayRef) -> Result<Vec<i64>> {
    Ok(match a.data_type() {
        DataType::Int8 => a.as_primitive::<Int8Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::Int16 => a.as_primitive::<Int16Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::Int32 => a.as_primitive::<Int32Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::Int64 => a.as_primitive::<Int64Type>().values().to_vec(),
        DataType::UInt8 => a.as_primitive::<UInt8Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::UInt16 => a.as_primitive::<UInt16Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::UInt32 => a.as_primitive::<UInt32Type>().values().iter().map(|&v| v as i64).collect(),
        DataType::UInt64 => a.as_primitive::<UInt64Type>().values().iter().map(|&v| v as i64).collect(),
        t => return err(format!("expected an integer column, got {t}")),
    })
}

/// A string column (plain, large, view or dictionary-encoded) as a per-row accessor.
pub enum Strs<'a> {
    Plain(&'a arrow_array::StringArray),
    Large(&'a arrow_array::LargeStringArray),
    View(&'a arrow_array::StringViewArray),
    Dict(Vec<i64>, Box<Strs<'a>>),
}

impl<'a> Strs<'a> {
    pub fn of(a: &'a ArrayRef) -> Result<Strs<'a>> {
        Ok(match a.data_type() {
            DataType::Utf8 => Strs::Plain(a.as_string::<i32>()),
            DataType::LargeUtf8 => Strs::Large(a.as_string::<i64>()),
            DataType::Utf8View => Strs::View(a.as_string_view()),
            DataType::Dictionary(_, _) => {
                let d = a.as_any_dictionary();
                let keys = ints(&(d.keys().slice(0, d.keys().len())))?;
                // `values()` is owned by the dictionary array, which outlives `self`.
                let values: &'a ArrayRef = unsafe { &*(d.values() as *const ArrayRef) };
                Strs::Dict(keys, Box::new(Strs::of(values)?))
            }
            t => return err(format!("expected a string column, got {t}")),
        })
    }

    pub fn get(&self, i: usize) -> &str {
        match self {
            Strs::Plain(a) => a.value(i),
            Strs::Large(a) => a.value(i),
            Strs::View(a) => a.value(i),
            Strs::Dict(k, v) => v.get(k[i] as usize),
        }
    }
}

/// `x` rendered with thousands separators.
pub fn commas(x: u64) -> String {
    let s = x.to_string();
    let mut out = String::new();
    for (i, c) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out
}

/// A raw pointer that may be shared across threads, for scatter writes to disjoint indices.
#[derive(Clone, Copy)]
pub struct Shared<T>(pub *mut T);
unsafe impl<T> Send for Shared<T> {}
unsafe impl<T> Sync for Shared<T> {}

impl<T> Shared<T> {
    /// The pointer (a method, so a closure captures the whole `Shared`, not its non-`Send` field).
    pub fn ptr(&self) -> *mut T {
        self.0
    }

    /// # Safety
    /// No two threads write the same index, and nothing reads it meanwhile.
    pub unsafe fn write(&self, i: usize, v: T) {
        *self.0.add(i) = v;
    }
}
