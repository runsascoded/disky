//! The merge layer (specs/vm-mode.md §3.2): shards' partial answers (`shard.rs`) → the view's tree.
//!
//! 1. A k-way merge of the shards' node lists by path (each sorted by `cmp_path`), summing each path's sides and
//!    children counts.
//! 2. A node some shard saw only part of, nonempty once summed, counts once toward its parent's children.
//! 3. The cell rule (`cell.rs`) against the summed total, top down: a node is drawn only under a drawn parent.
//! 4. The tree: drawn children by bytes (then name), and under each drawn node above the window's deepest level (and
//!    the folder) an `(other)` holding the exact rest, with `folded` = its nonempty children not drawn.
//!
//! The cut is final only here: it needs the view's total, which no shard has alone.

use std::cmp::{Ordering, Reverse};
use std::collections::{BinaryHeap, HashMap};

use serde::Serialize;

use crate::cell::Cut;
use crate::scan::Answer;
use crate::shard::{cmp_path, SNode, ShardAnswer};

/// A node of the answer's tree, in `/api/subtree`'s `ViewNode` shape (`site/functions/_lib/view.ts`); a diff's adds
/// the `to` side.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct TreeNode {
    pub name: String,
    pub kind: &'static str,
    pub bytes: u64,
    pub objects: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_to: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub objects_to: Option<u64>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub matched: bool,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub children: Vec<TreeNode>,
    /// An `(other)`: how many nonempty children it stands for.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub folded: Option<u64>,
}

pub const OTHER: &str = "(other)";

#[derive(Clone, Debug, Serialize)]
pub struct Tree {
    pub path: String,
    pub diff: bool,
    /// `[bytes a, objects a, bytes b, objects b]`.
    pub total: [u64; 4],
    /// Drawn nodes (the folder and `(other)`s not counted).
    pub nodes: usize,
    pub root: TreeNode,
}

struct Key(String, usize, usize);

impl PartialEq for Key {
    fn eq(&self, o: &Key) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Key {}
impl PartialOrd for Key {
    fn partial_cmp(&self, o: &Key) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Key {
    fn cmp(&self, o: &Key) -> Ordering {
        cmp_path(&self.0, &o.0).then(self.1.cmp(&o.1))
    }
}

/// Step 1: the shards' nodes as one list in path order, each path's pieces summed (`partial`: some shard saw only
/// part of it).
fn kway(answers: &[ShardAnswer]) -> Vec<SNode> {
    let mut heap: BinaryHeap<Reverse<Key>> = BinaryHeap::new();
    for (s, a) in answers.iter().enumerate() {
        if let Some(n) = a.nodes.first() {
            heap.push(Reverse(Key(n.path.clone(), s, 0)));
        }
    }
    let mut out: Vec<SNode> = Vec::new();
    while let Some(Reverse(Key(_, s, i))) = heap.pop() {
        let n = &answers[s].nodes[i];
        match out.last_mut() {
            Some(m) if m.path == n.path => {
                for k in 0..2 {
                    m.a[k] += n.a[k];
                    m.b[k] += n.b[k];
                }
                m.kids += n.kids;
                m.partial |= n.partial;
                m.matched |= n.matched;
            }
            _ => out.push(n.clone()),
        }
        if let Some(x) = answers[s].nodes.get(i + 1) {
            heap.push(Reverse(Key(x.path.clone(), s, i + 1)));
        }
    }
    out
}

fn parent_of(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(p, _)| p)
}

/// The view's tree from its shards' partial answers (`path`: the folder; `diff`: answer both sides).
pub fn merge(answers: Vec<ShardAnswer>, cut: &Cut, path: &str, diff: bool) -> Tree {
    let mut total = [0u64; 4];
    let mut root_kids = 0u64;
    for a in &answers {
        for k in 0..4 {
            total[k] += a.total[k];
        }
        root_kids += a.root_kids;
    }
    let mut nodes = kway(&answers);
    let at: HashMap<String, usize> = nodes.iter().enumerate().map(|(i, n)| (n.path.clone(), i)).collect();
    let nonzero = |n: &SNode| n.a != [0, 0] || n.b != [0, 0];
    // Step 2.
    let mut add: Vec<(Option<usize>, u64)> = Vec::new();
    for n in nodes.iter().filter(|n| n.partial && nonzero(n)) {
        add.push((if n.depth == cut.dp + 1 { None } else { at.get(parent_of(&n.path)).copied() }, 1));
    }
    for (p, k) in add {
        match p {
            None => root_kids += k,
            Some(i) => nodes[i].kids += k,
        }
    }
    // Step 3: nodes are in path order, so a parent's verdict precedes its children's.
    let g = total[0].max(total[2]);
    let mut drawn = vec![false; nodes.len()];
    let mut kids: Vec<Vec<usize>> = vec![Vec::new(); nodes.len()];
    let mut top: Vec<usize> = Vec::new();
    for i in 0..nodes.len() {
        let n = &nodes[i];
        if !nonzero(n) || !cut.keep(n.a[0].max(n.b[0]), n.depth, g) {
            continue;
        }
        if n.depth == cut.dp + 1 {
            drawn[i] = true;
            top.push(i);
        } else if let Some(&p) = at.get(parent_of(&n.path)) {
            if drawn[p] {
                drawn[i] = true;
                kids[p].push(i);
            }
        }
    }
    let count = drawn.iter().filter(|&&d| d).count();
    let side = |a: [u64; 2], b: [u64; 2], name: String, kind: &'static str, matched: bool| TreeNode {
        name,
        kind,
        bytes: a[0],
        objects: a[1],
        bytes_to: diff.then_some(b[0]),
        objects_to: diff.then_some(b[1]),
        matched,
        children: Vec::new(),
        folded: None,
    };
    let maxd = cut.maxd();
    // Step 4.
    fn build(
        i: Option<usize>,
        nodes: &[SNode],
        kids: &[Vec<usize>],
        top: &[usize],
        own: TreeNode,
        nk: u64,
        depth: u8,
        maxd: u8,
        side: &dyn Fn([u64; 2], [u64; 2], String, &'static str, bool) -> TreeNode,
    ) -> TreeNode {
        let mut t = own;
        let list = match i {
            None => top,
            Some(i) => &kids[i],
        };
        let mut ch: Vec<TreeNode> = list
            .iter()
            .map(|&k| {
                let n = &nodes[k];
                let name = n.path.rsplit('/').next().unwrap_or("").to_string();
                let own = side(n.a, n.b, name, if n.file { "file" } else { "dir" }, n.matched);
                build(Some(k), nodes, kids, top, own, n.kids, n.depth, maxd, side)
            })
            .collect();
        let big = |x: &TreeNode| x.bytes.max(x.bytes_to.unwrap_or(0));
        ch.sort_by(|x, y| big(y).cmp(&big(x)).then_with(|| x.name.cmp(&y.name)));
        if depth < maxd {
            let (mut ra, mut rb) = ([t.bytes, t.objects], [t.bytes_to.unwrap_or(0), t.objects_to.unwrap_or(0)]);
            for c in &ch {
                let sub = |x: &mut u64, y: u64| *x = x.checked_sub(y).expect("drawn children exceed their parent");
                sub(&mut ra[0], c.bytes);
                sub(&mut ra[1], c.objects);
                sub(&mut rb[0], c.bytes_to.unwrap_or(0));
                sub(&mut rb[1], c.objects_to.unwrap_or(0));
            }
            if ra != [0, 0] || rb != [0, 0] {
                let mut o = side(ra, rb, OTHER.to_string(), "dir", false);
                o.folded = Some(nk.saturating_sub(ch.len() as u64));
                ch.push(o);
            }
        }
        t.children = ch;
        t
    }
    let name = path.rsplit('/').next().unwrap_or("").to_string();
    let root = side([total[0], total[1]], [total[2], total[3]], name, "dir", false);
    let root = build(None, &nodes, &kids, &top, root, root_kids, cut.dp, maxd, &side);
    Tree { path: path.to_string(), diff, total, nodes: count, root }
}

impl Tree {
    /// The bench's flat answer: every drawn node's path and sides, and each `(other)`'s as its parent's rest.
    pub fn answer(&self, id: &str) -> Answer {
        let mut nodes = Vec::with_capacity(self.nodes);
        let mut rest = Vec::new();
        fn walk(t: &TreeNode, path: &str, nodes: &mut Vec<(String, u64, u64, u64, u64)>, rest: &mut Vec<(String, i64, i64, i64, i64)>) {
            for c in &t.children {
                if c.folded.is_some() && c.name == OTHER {
                    rest.push((path.to_string(), c.bytes as i64, c.objects as i64, c.bytes_to.unwrap_or(0) as i64, c.objects_to.unwrap_or(0) as i64));
                    continue;
                }
                let p = if path.is_empty() { c.name.clone() } else { format!("{path}/{}", c.name) };
                nodes.push((p.clone(), c.bytes, c.objects, c.bytes_to.unwrap_or(0), c.objects_to.unwrap_or(0)));
                walk(c, &p, nodes, rest);
            }
        }
        walk(&self.root, &self.path, &mut nodes, &mut rest);
        nodes.sort();
        rest.sort();
        Answer { id: id.to_string(), total: self.total, nodes, rest }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn n(path: &str, a: [u64; 2], kids: u64, partial: bool) -> SNode {
        SNode { path: path.into(), depth: path.split('/').count() as u8, file: false, matched: false, partial, a, b: [0, 0], kids }
    }

    #[test]
    fn pieces_sum_and_count_once() {
        // `a` split across two shards (one child whole in the first), `a/y` split too.
        let s1 = ShardAnswer { total: [60, 3, 0, 0], root_kids: 0, nodes: vec![n("a", [60, 2], 1, true), n("a/x", [40, 1], 0, false), n("a/y", [20, 1], 0, true)] };
        let s2 = ShardAnswer { total: [40, 3, 0, 0], root_kids: 1, nodes: vec![n("a", [30, 1], 0, true), n("a/y", [20, 1], 0, true), n("b", [10, 2], 0, false)] };
        let cut = Cut::ppm(0, 2, 150_000);
        let t = merge(vec![s1, s2], &cut, "", false);
        // a = 90 (children: x whole, y counted once here); b (10) is under 15% of 100: the root's `(other)`.
        let a = t.answer("q");
        assert_eq!(a.total, [100, 6, 0, 0]);
        assert_eq!(a.nodes, vec![("a".into(), 90, 3, 0, 0), ("a/x".into(), 40, 1, 0, 0), ("a/y".into(), 40, 2, 0, 0)]);
        assert_eq!(a.rest, vec![("".into(), 10, 3, 0, 0), ("a".into(), 10, 0, 0, 0)]);
        let folds = |t: &TreeNode| t.children.iter().map(|c| (c.name.clone(), c.folded)).collect::<Vec<_>>();
        assert_eq!(folds(&t.root), vec![("a".to_string(), None), (OTHER.to_string(), Some(1))]);
        assert_eq!(folds(&t.root.children[0]), vec![("x".to_string(), None), ("y".to_string(), None), (OTHER.to_string(), Some(0))]);
    }
}
