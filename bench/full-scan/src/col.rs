//! A column: owned (built by the loader) or a slice of a mapped pack file (`pack.rs`), read the same way.

use std::ops::Deref;

pub enum Col<T: Copy + 'static> {
    Own(Vec<T>),
    /// Points into a `memmap2::Mmap` that the owning index keeps alive.
    Map(*const T, usize),
}

unsafe impl<T: Copy + Send> Send for Col<T> {}
unsafe impl<T: Copy + Sync> Sync for Col<T> {}

impl<T: Copy> Deref for Col<T> {
    type Target = [T];
    #[inline(always)]
    fn deref(&self) -> &[T] {
        match self {
            Col::Own(v) => v,
            Col::Map(p, n) => unsafe { std::slice::from_raw_parts(*p, *n) },
        }
    }
}

impl<T: Copy> From<Vec<T>> for Col<T> {
    fn from(v: Vec<T>) -> Col<T> {
        Col::Own(v)
    }
}

impl<T: Copy> Default for Col<T> {
    fn default() -> Col<T> {
        Col::Own(Vec::new())
    }
}

impl<T: Copy> Col<T> {
    pub fn bytes(&self) -> u64 {
        std::mem::size_of_val::<[T]>(self) as u64
    }
}
