//! L1 内核：工作区内容检索（grep 语义）。**零 pyo3 依赖**（23 号文档的 5.2 规则 1）。
//!
//! ## regex 支持子集（25 号文档的 G0-B 判据）
//!
//! 只支持 `regex` crate 的语法。Python `re` 里**有**、`regex` 里**没有**的回溯类构造
//! （lookaround / 反向引用 / 原子组）一律**指名报错**（[`UnsupportedSyntax`]），
//! **绝不静默降级、绝不静默换实现**。
//!
//! ## 两套扫描语义（有意分开，别混用）
//!
//! - [`search_dir`]：**扁平命中列表**，按路径字节序、`max_results` 按行数计。差分测试的基准。
//! - [`search_tree`]：**按文件分组**，目录按小写名排序、`file_glob` 过滤、
//!   `max_files_with_hits` 按"有命中的文件数"计、标签相对 `label_root`。
//!   这一套对齐 `workspace_search` 工具的既有契约（见 25 号文档的 F9）。

use endless_kernel_core::Capability;
use glob::Pattern;
use regex::Regex;
use std::path::{Path, PathBuf};

/// 二进制探测窗口（与 Python 参考实现一致：首 8 KiB 含 NUL 视为二进制）。
const BINARY_SNIFF_BYTES: usize = 8192;

/// 一次检索命中。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hit {
    /// 相对检索根的路径，**用 `/` 分隔**（跨平台稳定，供差分比对）。
    pub path: String,
    /// 行号，从 1 开始。
    pub line_number: usize,
    pub line_text: String,
}

/// 不支持的语法构造（G0-B：**指名**报错）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnsupportedSyntax {
    /// 人类可读的构造名，例如 `lookbehind`。
    pub construct: String,
    /// 触发判定的字面量，例如 `(?<=`。
    pub needle: String,
}

/// 模式编译失败的两类原因。**分开**是为了让上层能分别处理：
/// 不支持的语法是"用户/模型用错了子集"，非法语法是"模式本身写错了"。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PatternError {
    Unsupported(UnsupportedSyntax),
    Invalid(String),
}

impl std::fmt::Display for PatternError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PatternError::Unsupported(u) => write!(
                f,
                "unsupported_pattern_syntax: {} is not supported by the regex subset (found {:?})",
                u.construct, u.needle
            ),
            PatternError::Invalid(msg) => write!(f, "invalid_pattern: {msg}"),
        }
    }
}

impl std::error::Error for PatternError {}

/// 已知不支持的构造表（字面量 → 名字）。
///
/// **顺序必须与 Python 参考实现的 `UNSUPPORTED_CONSTRUCTS` 完全一致** ——
/// 先命中哪个决定报哪个名字，顺序不同会让差分在"多构造同时出现"时报不同名字。
const UNSUPPORTED: &[(&str, &str)] = &[
    ("(?<=", "lookbehind"),
    ("(?<!", "negative lookbehind"),
    ("(?=", "lookahead"),
    ("(?!", "negative lookahead"),
    ("(?>", "atomic group"),
    ("(?P=", "named backreference"),
];

/// 判定模式里是否含不支持的构造。
pub fn classify_unsupported(pattern: &str) -> Option<UnsupportedSyntax> {
    for (needle, name) in UNSUPPORTED {
        if pattern.contains(needle) {
            return Some(UnsupportedSyntax {
                construct: (*name).to_string(),
                needle: (*needle).to_string(),
            });
        }
    }

    // 数字反向引用：`\` 后紧跟 1..9，且这个 `\` 本身没有被转义。
    let bytes = pattern.as_bytes();
    let mut i = 0usize;
    while i + 1 < bytes.len() {
        if bytes[i] == b'\\' {
            let mut run = 0usize;
            while i + run < bytes.len() && bytes[i + run] == b'\\' {
                run += 1;
            }
            let next = i + run;
            if run % 2 == 1 && next < bytes.len() && (b'1'..=b'9').contains(&bytes[next]) {
                let needle = pattern[i..=next].to_string();
                return Some(UnsupportedSyntax {
                    construct: "backreference".to_string(),
                    needle,
                });
            }
            i += run;
            continue;
        }
        i += 1;
    }

    None
}

/// 编译模式：**先查不支持构造，再交给 `regex`**。
pub fn compile_pattern(pattern: &str, case_sensitive: bool) -> Result<Regex, PatternError> {
    if let Some(unsupported) = classify_unsupported(pattern) {
        return Err(PatternError::Unsupported(unsupported));
    }
    let full = if case_sensitive {
        pattern.to_string()
    } else {
        format!("(?i){pattern}")
    };
    Regex::new(&full).map_err(|e| PatternError::Invalid(e.to_string()))
}

/// 行边界字符集合（对齐 Python `str.splitlines()`）。
fn is_line_boundary(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{0b}'
            | '\u{0c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// 按 Python `str.splitlines()` 的语义切行。
pub fn split_lines(text: &str) -> Vec<&str> {
    let mut out: Vec<&str> = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < text.len() {
        let c = match text[i..].chars().next() {
            Some(c) => c,
            None => break,
        };
        let clen = c.len_utf8();
        let boundary_len = if c == '\r' {
            if text[i + clen..].starts_with('\n') {
                clen + 1
            } else {
                clen
            }
        } else if is_line_boundary(c) {
            clen
        } else {
            0
        };
        if boundary_len > 0 {
            out.push(&text[start..i]);
            i += boundary_len;
            start = i;
        } else {
            i += clen;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// 扁平检索选项（[`search_dir`] 用）。
#[derive(Debug, Clone)]
pub struct SearchOptions {
    pub max_results: usize,
    pub max_file_bytes: u64,
    pub max_hits_per_file: usize,
    pub ignore_dirs: Vec<String>,
    /// 文件名后缀白名单；空 vec = 不过滤。
    pub file_suffixes: Vec<String>,
}

impl Default for SearchOptions {
    fn default() -> Self {
        Self {
            max_results: 50,
            max_file_bytes: 1_000_000,
            max_hits_per_file: 200,
            ignore_dirs: default_ignore_dirs(),
            file_suffixes: Vec::new(),
        }
    }
}

/// 扁平检索结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SearchOutcome {
    pub hits: Vec<Hit>,
    pub files_scanned: usize,
    pub files_skipped: usize,
    /// 是否因为某个上限而**丢掉了**命中。
    pub truncated: bool,
}

/// 本内核的能力自证（23 号文档的 5.2 规则 6：如实报告，绝不静默降级）。
pub fn capabilities() -> Vec<Capability> {
    vec![
        Capability::ok("search.line_scan", "递归目录遍历 + 逐行匹配"),
        Capability::ok(
            "search.regex_subset",
            "Rust regex 子集；回溯类构造被指名拒绝",
        ),
        Capability::ok(
            "search.splitlines_parity",
            "行切分对齐 Python str.splitlines()",
        ),
        Capability::ok(
            "search.tree_grouped",
            "按文件分组的树形检索（对齐 workspace_search 契约）",
        ),
    ]
}

fn default_ignore_dirs() -> Vec<String> {
    [
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        ".hg",
        ".svn",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect()
}

fn suffix_allowed(path: &Path, suffixes: &[String]) -> bool {
    if suffixes.is_empty() {
        return true;
    }
    match path.file_name().and_then(|n| n.to_str()) {
        Some(name) => suffixes.iter().any(|s| name.ends_with(s.as_str())),
        None => false,
    }
}

/// 递归收集候选文件，**按路径排序**（保证结果顺序确定，差分才可比）。
fn collect_files(dir: &Path, opts: &SearchOptions, out: &mut Vec<PathBuf>, skipped: &mut usize) {
    let mut entries: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(read_dir) => read_dir.filter_map(|e| e.ok()).map(|e| e.path()).collect(),
        Err(_) => {
            *skipped += 1;
            return;
        }
    };
    entries.sort();
    for path in entries {
        let meta = match std::fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => {
                *skipped += 1;
                continue;
            }
        };
        if meta.is_dir() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !opts.ignore_dirs.iter().any(|d| d == name) {
                collect_files(&path, opts, out, skipped);
            }
            continue;
        }
        if meta.is_file() && suffix_allowed(&path, &opts.file_suffixes) {
            out.push(path);
        }
    }
}

/// 在 `root` 下做内容检索（扁平命中列表）。
pub fn search_dir(root: &Path, pattern: &Regex, opts: &SearchOptions) -> SearchOutcome {
    let mut files: Vec<PathBuf> = Vec::new();
    let mut files_skipped = 0usize;
    collect_files(root, opts, &mut files, &mut files_skipped);

    let mut hits: Vec<Hit> = Vec::new();
    let mut files_scanned = 0usize;
    let mut truncated = false;
    let mut stop = false;

    for path in files {
        if stop {
            break;
        }
        let meta = match std::fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => {
                files_skipped += 1;
                continue;
            }
        };
        if meta.len() > opts.max_file_bytes {
            files_skipped += 1;
            continue;
        }
        let raw = match std::fs::read(&path) {
            Ok(raw) => raw,
            Err(_) => {
                files_skipped += 1;
                continue;
            }
        };
        let head_len = raw.len().min(BINARY_SNIFF_BYTES);
        if raw[..head_len].contains(&0u8) {
            files_skipped += 1;
            continue;
        }
        let text = match std::str::from_utf8(&raw) {
            Ok(text) => text,
            Err(_) => {
                files_skipped += 1;
                continue;
            }
        };
        files_scanned += 1;

        let rel = match path.strip_prefix(root) {
            Ok(rel) => rel.to_path_buf(),
            Err(_) => path.clone(),
        };
        let rel_str = rel.to_string_lossy().replace('\\', "/");

        let mut per_file = 0usize;
        for (idx, line) in split_lines(text).into_iter().enumerate() {
            if per_file >= opts.max_hits_per_file {
                truncated = true;
                break;
            }
            if !pattern.is_match(line) {
                continue;
            }
            hits.push(Hit {
                path: rel_str.clone(),
                line_number: idx + 1,
                line_text: line.to_string(),
            });
            per_file += 1;
            if hits.len() >= opts.max_results {
                truncated = true;
                stop = true;
                break;
            }
        }
    }

    SearchOutcome {
        hits,
        files_scanned,
        files_skipped,
        truncated,
    }
}

/// 树形检索选项：对齐 `workspace_search` 工具的既有语义（25 号文档的 F9）。
#[derive(Debug, Clone)]
pub struct TreeOptions {
    /// 上限按**有命中的文件数**计（不是行数）—— 这是工具既有语义。
    pub max_files_with_hits: usize,
    pub max_hits_per_file: usize,
    pub max_file_bytes: u64,
    pub ignore_dirs: Vec<String>,
    /// 文件名 glob（`*.md` 等）；None = 不过滤。
    pub file_glob: Option<String>,
}

impl Default for TreeOptions {
    fn default() -> Self {
        Self {
            max_files_with_hits: 50,
            max_hits_per_file: 200,
            max_file_bytes: 1_000_000,
            ignore_dirs: default_ignore_dirs(),
            file_glob: None,
        }
    }
}

/// 一个文件里的命中。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileHits {
    /// 相对 `label_root`、以 `/` 分隔。
    pub path: String,
    /// (行号, 行文本)，行号从 1 开始。
    pub matches: Vec<(usize, String)>,
}

/// 树形检索结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeOutcome {
    pub files: Vec<FileHits>,
    pub files_scanned: usize,
    pub truncated: bool,
}

fn glob_allows(matcher: &Option<Pattern>, name: &str) -> bool {
    match matcher {
        None => true,
        Some(pattern) => pattern.matches(name),
    }
}

/// 标签相对 `label_root`（工作区根）；越界时退化为文件名。
fn label_for(label_root: &Path, path: &Path) -> String {
    match path.strip_prefix(label_root) {
        Ok(rel) => rel.to_string_lossy().replace('\\', "/"),
        Err(_) => path
            .file_name()
            .map(|n| n.to_string_lossy().to_string())
            .unwrap_or_default(),
    }
}

/// 递归收集候选文件；目录按**小写名**排序（对齐工具的 `name.lower()` 排序）。
fn collect_tree(
    dir: &Path,
    opts: &TreeOptions,
    matcher: &Option<Pattern>,
    out: &mut Vec<PathBuf>,
) {
    let mut entries: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(read_dir) => read_dir.filter_map(|e| e.ok()).map(|e| e.path()).collect(),
        Err(_) => return,
    };
    entries.sort_by_key(|p| {
        p.file_name()
            .map(|n| n.to_string_lossy().to_lowercase())
            .unwrap_or_default()
    });
    for path in entries {
        let meta = match std::fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if meta.is_dir() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if !opts.ignore_dirs.iter().any(|d| d == name) {
                collect_tree(&path, opts, matcher, out);
            }
            continue;
        }
        if meta.is_file() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if glob_allows(matcher, name) {
                out.push(path);
            }
        }
    }
}

/// 树形检索：**按文件分组**，标签相对 `label_root`。
///
/// `scope` 可以是**文件**或目录；是文件时只扫它自己（仍受 glob 约束）。
pub fn search_tree(
    scope: &Path,
    label_root: &Path,
    pattern: &Regex,
    opts: &TreeOptions,
) -> TreeOutcome {
    let matcher = opts
        .file_glob
        .as_ref()
        .and_then(|g| Pattern::new(g).ok());

    let mut candidates: Vec<PathBuf> = Vec::new();
    if scope.is_file() {
        let name = scope.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if glob_allows(&matcher, name) {
            candidates.push(scope.to_path_buf());
        }
    } else {
        collect_tree(scope, opts, &matcher, &mut candidates);
    }

    let mut files: Vec<FileHits> = Vec::new();
    let mut files_scanned = 0usize;
    let mut truncated = false;

    for path in candidates {
        if files.len() >= opts.max_files_with_hits {
            truncated = true;
            break;
        }
        let meta = match std::fs::metadata(&path) {
            Ok(meta) => meta,
            Err(_) => continue,
        };
        if meta.len() > opts.max_file_bytes {
            continue;
        }
        let raw = match std::fs::read(&path) {
            Ok(raw) => raw,
            Err(_) => continue,
        };
        let head_len = raw.len().min(BINARY_SNIFF_BYTES);
        if raw[..head_len].contains(&0u8) {
            continue;
        }
        let text = match std::str::from_utf8(&raw) {
            Ok(text) => text,
            Err(_) => continue,
        };
        files_scanned += 1;

        let label = label_for(label_root, &path);
        let mut matches: Vec<(usize, String)> = Vec::new();
        for (idx, line) in split_lines(text).into_iter().enumerate() {
            if matches.len() >= opts.max_hits_per_file {
                truncated = true;
                break;
            }
            if pattern.is_match(line) {
                matches.push((idx + 1, line.to_string()));
            }
        }
        if !matches.is_empty() {
            files.push(FileHits { path: label, matches });
        }
    }

    TreeOutcome {
        files,
        files_scanned,
        truncated,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TempTree {
        root: PathBuf,
    }

    impl TempTree {
        fn new(tag: &str) -> Self {
            let mut root = std::env::temp_dir();
            root.push(format!("endless-search-test-{}-{}", std::process::id(), tag));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).expect("create temp root");
            Self { root }
        }

        fn write(&self, rel: &str, content: &str) {
            let path = self.root.join(rel);
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent).expect("create parent");
            }
            std::fs::write(path, content).expect("write file");
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn split_lines_matches_python_semantics() {
        assert_eq!(Vec::<&str>::new(), split_lines(""));
        assert_eq!(vec!["abc"], split_lines("abc"));
        assert_eq!(vec!["abc"], split_lines("abc\n"));
        assert_eq!(vec!["a", "", "b"], split_lines("a\n\nb"));
        assert_eq!(vec!["a", "b"], split_lines("a\r\nb"));
        assert_eq!(vec!["a", "b", "c"], split_lines("a\u{0b}b\u{0c}c"));
        assert_eq!(vec!["a", "b", "c"], split_lines("a\u{2028}b\u{2029}c"));
        assert_eq!(vec!["a", "b"], split_lines("a\u{85}b"));
    }

    #[test]
    fn classify_unsupported_names_each_construct() {
        let cases = [
            ("(?=x)", "lookahead"),
            ("(?!x)", "negative lookahead"),
            ("(?<=x)", "lookbehind"),
            ("(?<!x)", "negative lookbehind"),
            ("(?>x)", "atomic group"),
            ("(?P=name)", "named backreference"),
            ("(a)\\1", "backreference"),
        ];
        for (pattern, expected) in cases {
            let found = classify_unsupported(pattern).expect(pattern);
            assert_eq!(expected, found.construct, "{pattern}");
        }
    }

    #[test]
    fn classify_unsupported_allows_supported_subset() {
        for pattern in ["^abc$", "a|b", "[0-9]+", "\\d{2,4}", "(?:x)", "(?P<n>x)", "\\."] {
            assert!(
                classify_unsupported(pattern).is_none(),
                "should be supported: {pattern}"
            );
        }
    }

    #[test]
    fn compile_pattern_rejects_backtracking_by_name() {
        let err = compile_pattern("(?<=a)b", true).unwrap_err();
        let rendered = err.to_string();
        match &err {
            PatternError::Unsupported(u) => assert_eq!("lookbehind", u.construct),
            other => panic!("expected Unsupported, got {other:?}"),
        }
        assert!(rendered.contains("unsupported_pattern_syntax"));
    }

    #[test]
    fn compile_pattern_reports_invalid_separately() {
        let err = compile_pattern("(unclosed", true).unwrap_err();
        match err {
            PatternError::Invalid(_) => {}
            other => panic!("expected Invalid, got {other:?}"),
        }
    }

    #[test]
    fn compile_pattern_is_case_insensitive_when_asked() {
        let re = compile_pattern("hello", false).expect("compile");
        assert!(re.is_match("HELLO"));
        let re = compile_pattern("hello", true).expect("compile");
        assert!(!re.is_match("HELLO"));
    }

    #[test]
    fn search_dir_finds_hits_in_sorted_order() {
        let tree = TempTree::new("hits");
        tree.write("b.txt", "alpha\nbeta\n");
        tree.write("a.txt", "beta\nalpha\n");
        let re = compile_pattern("beta", true).expect("compile");
        let outcome = search_dir(&tree.root, &re, &SearchOptions::default());

        let got: Vec<(String, usize, String)> = outcome
            .hits
            .iter()
            .map(|h| (h.path.clone(), h.line_number, h.line_text.clone()))
            .collect();
        assert_eq!(
            vec![
                ("a.txt".to_string(), 1, "beta".to_string()),
                ("b.txt".to_string(), 2, "beta".to_string()),
            ],
            got
        );
        assert_eq!(2, outcome.files_scanned);
        assert!(!outcome.truncated);
    }

    #[test]
    fn search_dir_skips_ignored_dirs_binary_and_oversized() {
        let tree = TempTree::new("skips");
        tree.write("keep.txt", "target\n");
        tree.write(".git/config", "target\n");
        tree.write("big.txt", &("x".repeat(64) + "\ntarget\n"));
        std::fs::write(
            tree.root.join("bin.dat"),
            [0u8, 1, 2, 3, b't', b'a', b'r', b'g', b'e', b't'],
        )
        .expect("write binary");

        let mut opts = SearchOptions::default();
        opts.max_file_bytes = 16;
        let re = compile_pattern("target", true).expect("compile");
        let outcome = search_dir(&tree.root, &re, &opts);

        let paths: Vec<String> = outcome.hits.iter().map(|h| h.path.clone()).collect();
        assert_eq!(vec!["keep.txt".to_string()], paths);
        assert_eq!(2, outcome.files_skipped);
        assert_eq!(1, outcome.files_scanned);
    }

    #[test]
    fn search_dir_truncates_at_max_results() {
        let tree = TempTree::new("truncate");
        tree.write("many.txt", "hit\nhit\nhit\nhit\n");
        let mut opts = SearchOptions::default();
        opts.max_results = 2;
        let re = compile_pattern("hit", true).expect("compile");
        let outcome = search_dir(&tree.root, &re, &opts);

        assert_eq!(2, outcome.hits.len());
        assert!(outcome.truncated);
    }

    #[test]
    fn search_dir_honours_per_file_cap() {
        let tree = TempTree::new("perfile");
        tree.write("many.txt", "hit\nhit\nhit\n");
        let mut opts = SearchOptions::default();
        opts.max_hits_per_file = 1;
        let re = compile_pattern("hit", true).expect("compile");
        let outcome = search_dir(&tree.root, &re, &opts);

        assert_eq!(1, outcome.hits.len());
        assert!(outcome.truncated);
    }

    #[test]
    fn capabilities_are_reported_truthfully() {
        let caps = capabilities();
        assert!(!caps.is_empty());
        assert!(caps.iter().all(|c| c.available));
        assert!(caps.iter().any(|c| c.name == "search.regex_subset"));
    }

    #[test]
    fn search_tree_groups_by_file_and_labels_from_label_root() {
        let tree = TempTree::new("tree");
        tree.write("sub/a.txt", "beta\n");
        tree.write("b.txt", "beta\nbeta\n");
        let scope = tree.root.join("sub");
        let re = compile_pattern("beta", true).expect("compile");
        let outcome = search_tree(&scope, &tree.root, &re, &TreeOptions::default());

        assert_eq!(
            vec![FileHits {
                path: "sub/a.txt".to_string(),
                matches: vec![(1, "beta".to_string())],
            }],
            outcome.files
        );
        assert_eq!(1, outcome.files_scanned);
    }

    #[test]
    fn search_tree_honours_glob_and_case_insensitive_order() {
        let tree = TempTree::new("treeglob");
        tree.write("B.md", "hit\n");
        tree.write("a.md", "hit\n");
        tree.write("c.txt", "hit\n");
        let re = compile_pattern("hit", true).expect("compile");
        let mut opts = TreeOptions::default();
        opts.file_glob = Some("*.md".to_string());
        let outcome = search_tree(&tree.root, &tree.root, &re, &opts);

        // 小写名排序：a.md 在 B.md 之前；c.txt 被 glob 挡掉。
        let names: Vec<String> = outcome.files.iter().map(|f| f.path.clone()).collect();
        assert_eq!(vec!["a.md", "B.md"], names);
    }

    #[test]
    fn search_tree_caps_by_file_count_not_line_count() {
        let tree = TempTree::new("treecap");
        tree.write("a.txt", "hit\nhit\nhit\n");
        tree.write("b.txt", "hit\n");
        let re = compile_pattern("hit", true).expect("compile");
        let mut opts = TreeOptions::default();
        opts.max_files_with_hits = 1;
        let outcome = search_tree(&tree.root, &tree.root, &re, &opts);

        // 关键：上限是**文件数**，所以 a.txt 里 3 个命中都保留。
        assert_eq!(1, outcome.files.len());
        assert_eq!(3, outcome.files[0].matches.len());
        assert!(outcome.truncated);
    }

    #[test]
    fn search_tree_accepts_a_single_file_scope() {
        let tree = TempTree::new("treesingle");
        tree.write("only.txt", "hit\n");
        let re = compile_pattern("hit", true).expect("compile");
        let outcome = search_tree(&tree.root.join("only.txt"), &tree.root, &re, &TreeOptions::default());

        assert_eq!(1, outcome.files.len());
        assert_eq!("only.txt", outcome.files[0].path);
    }
}