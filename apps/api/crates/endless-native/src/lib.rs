//! L2 适配层：PyO3 薄绑定。本 crate 是**唯一**允许依赖 pyo3 的地方
//! L1 内核不依赖本层，本层不承载业务策略。
//! 本层只做类型转换、ABI 自检与能力自证；不做任何策略判断。
use endless_kernel_core::{check_abi, Capability, KERNEL_ABI_VERSION};
use pyo3::prelude::*;

/// 本扩展编译时的内核 ABI 版本。
#[pyfunction]
fn abi_version() -> u32 {
    KERNEL_ABI_VERSION
}

/// ABI 自检：不匹配即抛错（fail-closed，fifteen 号文档口径：绝不静默降级）。
#[pyfunction]
fn assert_abi(host: u32) -> PyResult<()> {
    check_abi(host).map_err(pyo3::exceptions::PyRuntimeError::new_err)
}

/// 能力自证：每行 "name|1|detail"（可用）或 "name|0|detail"（不可用）。
///
/// 刻意**不用** `Vec<(String, bool, String)>` 返回：在 pyo3 0.29.2 下它实测
/// 返回形状错乱且含空指针（一索引就段错误，exit 139）。
/// 改成纯字符串列表绕开元组转换，语义不变（由 Python 侧薄封装解析）。
#[pyfunction]
fn capabilities() -> Vec<String> {
    let a = Capability::ok("native.binding", "PyO3 binding loaded");
    let b = Capability::ok("core.manifest", "L1 kernel core available");
    let mut out: Vec<String> = Vec::new();
    for c in [a, b] {
        let flag = if c.available { "1" } else { "0" };
        out.push(c.name + "|" + flag + "|" + &c.detail);
    }
    out
}

/// Search kernel self-report: name|1|detail rows.
#[pyfunction]
fn search_capabilities() -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for c in endless_kernel_search::capabilities() {
        let flag = if c.available { "1" } else { "0" };
        out.push(c.name + "|" + flag + "|" + &c.detail);
    }
    out
}

/// Classify unsupported pattern syntax. Empty = supported,
/// otherwise construct + SEP + needle (never Option/tuple: see F5).
#[pyfunction]
fn search_classify_unsupported(pattern: String) -> String {
    match endless_kernel_search::classify_unsupported(&pattern) {
        None => String::new(),
        Some(u) => u.construct + "\u{1f}" + &u.needle,
    }
}

/// Search under root. Row 0 is the meta row, then one row per hit.
#[pyfunction]
fn search_workspace(
    root: String,
    pattern: String,
    case_sensitive: bool,
    max_results: usize,
    max_file_bytes: u64,
    max_hits_per_file: usize,
    file_suffixes: Vec<String>,
) -> PyResult<Vec<String>> {
    let compiled = endless_kernel_search::compile_pattern(&pattern, case_sensitive)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let mut options = endless_kernel_search::SearchOptions::default();
    options.max_results = max_results;
    options.max_file_bytes = max_file_bytes;
    options.max_hits_per_file = max_hits_per_file;
    options.file_suffixes = file_suffixes;
    let outcome = endless_kernel_search::search_dir(
        std::path::Path::new(&root), &compiled, &options);
    let separator = "\u{1f}";
    let mut rows: Vec<String> = Vec::new();
    let mut meta: Vec<String> = Vec::new();
    meta.push("meta".to_string());
    meta.push(outcome.files_scanned.to_string());
    meta.push(outcome.files_skipped.to_string());
    meta.push(outcome.truncated.to_string());
    rows.push(meta.join(separator));
    for hit in outcome.hits {
        let mut row: Vec<String> = Vec::new();
        row.push(hit.path);
        row.push(hit.line_number.to_string());
        row.push(hit.line_text);
        rows.push(row.join(separator));
    }
    Ok(rows)
}

/// Tree search (workspace_search contract). Rows:
/// - [0]: meta + SEP + files_scanned + SEP + truncated
/// - per file: file + SEP + path
/// - per hit : match + SEP + line_number + SEP + line_text
#[pyfunction]
#[pyo3(signature = (scope, label_root, pattern, case_sensitive, max_files_with_hits, max_file_bytes, max_hits_per_file, file_glob=None))]
fn search_tree_workspace(
    scope: String,
    label_root: String,
    pattern: String,
    case_sensitive: bool,
    max_files_with_hits: usize,
    max_file_bytes: u64,
    max_hits_per_file: usize,
    file_glob: Option<String>,
) -> PyResult<Vec<String>> {
    let compiled = endless_kernel_search::compile_pattern(&pattern, case_sensitive)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let mut options = endless_kernel_search::TreeOptions::default();
    options.max_files_with_hits = max_files_with_hits;
    options.max_file_bytes = max_file_bytes;
    options.max_hits_per_file = max_hits_per_file;
    options.file_glob = file_glob;
    let outcome = endless_kernel_search::search_tree(
        std::path::Path::new(&scope),
        std::path::Path::new(&label_root),
        &compiled,
        &options,
    );
    let separator = "\u{1f}";
    let mut rows: Vec<String> = Vec::new();
    let mut meta: Vec<String> = Vec::new();
    meta.push("meta".to_string());
    meta.push(outcome.files_scanned.to_string());
    meta.push(outcome.truncated.to_string());
    rows.push(meta.join(separator));
    for file in outcome.files {
        rows.push(["file".to_string(), file.path].join(separator));
        for (line_number, line_text) in file.matches {
            rows.push(["match".to_string(), line_number.to_string(), line_text].join(separator));
        }
    }
    Ok(rows)
}

/// 通过句柄读文件（逐段 O_NOFOLLOW）—— 闭合 TOCTOU（RK-3）。
/// 返回 Python `bytes`（用 `Bound<PyBytes>` 而不是 `Vec<u8>`：后者会被
/// pyo3 转成 `list[int]`，1 MB 文件就是一百万个 Python int）。
#[pyfunction]
fn fs_read_beneath<'py>(
    py: Python<'py>,
    root: String,
    rel: String,
    max_bytes: u64,
) -> PyResult<Bound<'py, pyo3::types::PyBytes>> {
    let data = endless_kernel_fs::read_beneath(std::path::Path::new(&root), &rel, max_bytes)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    Ok(pyo3::types::PyBytes::new(py, &data))
}


/// 覆盖写工作区文件（逐段 nofollow，全程句柄）。
/// 返回 None；失败抛 ValueError（消息形如 "code: detail"）。
#[pyfunction]
fn fs_write_beneath(root: String, rel: String, data: &[u8]) -> PyResult<()> {
    endless_kernel_fs::write_beneath(std::path::Path::new(&root), &rel, data)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

/// 删除工作区**文件**（unlinkat，不删目录）。
#[pyfunction]
fn fs_remove_beneath(root: String, rel: String) -> PyResult<()> {
    endless_kernel_fs::remove_beneath(std::path::Path::new(&root), &rel)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}


/// 按句柄打开工作区内文件，返回**裸 fd**（所有权移交给 Python）。
/// 用于需要流式读的场景：路径上再被换也影响不到这个 fd。
#[pyfunction]
fn fs_open_beneath(root: String, rel: String) -> PyResult<i32> {
    use std::os::fd::IntoRawFd;
    let fd = endless_kernel_fs::open_beneath(
        std::path::Path::new(&root),
        &rel,
        endless_kernel_fs::OpenMode::Read,
    )
    .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    Ok(fd.into_raw_fd())
}


/// 逐段建目录链（等价 Path.mkdir(parents=True, exist_ok=True)，但逐段 nofollow）。
#[pyfunction]
fn fs_mkdir_beneath(root: String, rel: String) -> PyResult<()> {
    endless_kernel_fs::mkdir_beneath(std::path::Path::new(&root), &rel)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}


/// 列目录。每行 "dir\u{1f}file\u{1f}symlink\u{1f}name"
/// —— 标志在前、名字在后：名字可能含分隔符，Python 侧 split(maxsplit=3) 才稳。
#[pyfunction]
fn fs_list_beneath(root: String, rel: String) -> PyResult<Vec<String>> {
    let entries = endless_kernel_fs::list_beneath(std::path::Path::new(&root), &rel)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    let separator = "\u{1f}";
    let mut rows: Vec<String> = Vec::new();
    for entry in entries {
        rows.push(
            [
                if entry.is_dir { "1" } else { "0" }.to_string(),
                if entry.is_file { "1" } else { "0" }.to_string(),
                if entry.is_symlink { "1" } else { "0" }.to_string(),
                entry.size.to_string(),
                entry.name,
            ]
            .join(separator),
        );
    }
    Ok(rows)
}

#[pymodule]
fn _native_core(m: &Bound<'_ , PyModule>) -> PyResult<()> {
    m.add("KERNEL_ABI_VERSION", KERNEL_ABI_VERSION)?;
    m.add_function(wrap_pyfunction!(abi_version, m)?)?;
    m.add_function(wrap_pyfunction!(assert_abi, m)?)?;
    m.add_function(wrap_pyfunction!(capabilities, m)?)?;
    m.add_function(wrap_pyfunction!(search_capabilities, m)?)?;
    m.add_function(wrap_pyfunction!(search_classify_unsupported, m)?)?;
    m.add_function(wrap_pyfunction!(search_workspace, m)?)?;
    m.add_function(wrap_pyfunction!(search_tree_workspace, m)?)?;
    m.add_function(wrap_pyfunction!(fs_read_beneath, m)?)?;
    m.add_function(wrap_pyfunction!(fs_write_beneath, m)?)?;
    m.add_function(wrap_pyfunction!(fs_remove_beneath, m)?)?;
    m.add_function(wrap_pyfunction!(fs_open_beneath, m)?)?;
    m.add_function(wrap_pyfunction!(fs_mkdir_beneath, m)?)?;
    m.add_function(wrap_pyfunction!(fs_list_beneath, m)?)?;
    Ok(())
}
