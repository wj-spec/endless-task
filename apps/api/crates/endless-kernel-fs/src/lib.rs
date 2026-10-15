//! L1 内核：工作区文件操作。**零 pyo3 依赖**（23 号文档的 5.2 规则 1）。
//!
//! ## 为什么需要它（RK-3 的立项依据）
//!
//! `path_safety.resolve_workspace_path` 返回的是 **Path（字符串快照）**，不是**句柄** ——
//! 回归场景：校验通过后把中间目录
//! 换成指向工作区外的 symlink，后续按 `canonical` 读取就**读到了区外内容**，
//! 而且**完全不需要竞态**。
//!
//! 根因是「校验」与「使用」是**两次独立的路径解析**。本内核把两者合并成一次：
//! **逐段 `openat` + `O_NOFOLLOW`**，任何一段是符号链接即拒绝，返回的是**句柄**。
//!
//! ## 语义边界（刻意保守）
//!
//! - 只接受**相对**路径，且拒绝 `..` 段；
//! - 中间段必须是目录；
//! - **每一段都带 `O_NOFOLLOW`** —— 连末段是 symlink 也拒（不做
//!   「symlink 指向工作区内就放行」的猜测：那需要**再解析一次**，等于把 TOCTOU 放回来）。

use std::os::fd::OwnedFd;
use std::path::Path;

use rustix::fs::{openat, Mode, OFlags, CWD};

/// 打开意图。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpenMode {
    Read,
    Write,
    /// 覆盖写：`O_WRONLY | O_CREAT | O_TRUNC`（**仍带 O_NOFOLLOW**）。
    Create,
}

/// 文件操作失败的原因。**分类**是为了让上层能分别处理并能**指名**报错。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FsError {
    /// 路径形态非法（空、绝对路径、含 `..`）。
    InvalidPath(String),
    /// 某一段是符号链接 —— 这是 TOCTOU 的核心防线。
    SymlinkSegment(String),
    NotFound(String),
    Denied(String),
    /// 中间段存在但不是目录（macOS 上 symlink 段也会走到这里 —— 见 `map_errno`）。
    NotADirectory(String),
    /// 超出调用方给的字节上限。
    TooLarge(u64),
    Io(String),
}

impl std::fmt::Display for FsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FsError::InvalidPath(detail) => write!(f, "invalid_path: {detail}"),
            FsError::SymlinkSegment(segment) => write!(
                f,
                "path_escape: symlink segment {segment:?} is not allowed"
            ),
            FsError::NotFound(detail) => write!(f, "path_not_found: {detail}"),
            FsError::Denied(detail) => write!(f, "permission_denied: {detail}"),
            FsError::NotADirectory(detail) => write!(f, "not_a_directory: {detail}"),
            FsError::TooLarge(size) => write!(f, "file_too_large: {size} bytes"),
            FsError::Io(detail) => write!(f, "io_error: {detail}"),
        }
    }
}

impl std::error::Error for FsError {}

fn map_errno(errno: rustix::io::Errno, segment: &str) -> FsError {
    match errno {
        rustix::io::Errno::LOOP => FsError::SymlinkSegment(segment.to_string()),
        rustix::io::Errno::NOENT => FsError::NotFound(segment.to_string()),
        rustix::io::Errno::ACCESS => FsError::Denied(segment.to_string()),
        rustix::io::Errno::NOTDIR => FsError::NotADirectory(segment.to_string()),
        other => FsError::Io(format!("{segment}: {other}")),
    }
}

/// 把相对路径切成段，并做**词法**拒绝。
///
/// 注意：这里的词法检查**不是**安全边界（真正的边界是 `open_beneath` 的逐段 `openat`），
/// 它只是为了让错误更早、更明确。
fn split_segments(rel: &str) -> Result<Vec<&str>, FsError> {
    if rel.is_empty() {
        return Err(FsError::InvalidPath("empty path".to_string()));
    }
    if rel.starts_with('/') || rel.starts_with('\\') {
        return Err(FsError::InvalidPath(
            "absolute paths are not accepted".to_string(),
        ));
    }
    let mut segments: Vec<&str> = Vec::new();
    for raw in rel.split(['/', '\\']) {
        if raw.is_empty() || raw == "." {
            continue;
        }
        if raw == ".." {
            return Err(FsError::InvalidPath(
                "parent segments are not accepted".to_string(),
            ));
        }
        segments.push(raw);
    }
    if segments.is_empty() {
        return Err(FsError::InvalidPath("empty path".to_string()));
    }
    Ok(segments)
}

/// 在工作区根下逐段打开，返回**句柄**。
///
/// 每一段都用 `openat(当前 dirfd, 段名, O_NOFOLLOW)` —— 于是：
/// - 任一环节是 symlink ⇒ `ELOOP` ⇒ [`FsError::SymlinkSegment`]；
/// - 中间段不是目录 ⇒ 失败；
/// - **不存在"校验完之后路径被换掉"的窗口**：句柄一旦拿到就锁定了那个 inode。
pub fn open_beneath(root: &Path, rel: &str, mode: OpenMode) -> Result<OwnedFd, FsError> {
    let segments = split_segments(rel)?;

    let root_fd = openat(CWD, root, OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())
        .map_err(|errno| map_errno(errno, "<root>"))?;

    let last_index = segments.len() - 1;
    let mut current: OwnedFd = root_fd;
    for (index, segment) in segments.iter().enumerate() {
        let mut flags = OFlags::CLOEXEC | OFlags::NOFOLLOW;
        if index == last_index {
            flags |= match mode {
                OpenMode::Read => OFlags::RDONLY,
                OpenMode::Write => OFlags::RDWR,
                OpenMode::Create => OFlags::WRONLY | OFlags::CREATE | OFlags::TRUNC,
            };
        } else {
            flags |= OFlags::DIRECTORY | OFlags::RDONLY;
        }
        // ⚠️ 创建时必须给权限位：Mode::empty() 会建出 0000 的文件，
        // 随后连自己都读不了（实测报 Denied）。仅在 Create 时给 0644；
        // 对已存在的文件 POSIX 忽略 mode，所以传了也无害。
        let create_mode = if matches!(mode, OpenMode::Create) {
            Mode::RUSR | Mode::WUSR | Mode::RGRP | Mode::ROTH
        } else {
            Mode::empty()
        };
        current = openat(&current, *segment, flags, create_mode)
            .map_err(|errno| map_errno(errno, segment))?;
    }
    Ok(current)
}

/// 读一个文件，且**全程只用句柄**（不再碰路径）。
pub fn read_beneath(root: &Path, rel: &str, max_bytes: u64) -> Result<Vec<u8>, FsError> {
    let fd = open_beneath(root, rel, OpenMode::Read)?;
    let mut file = std::fs::File::from(fd);
    let meta = file
        .metadata()
        .map_err(|error| FsError::Io(error.to_string()))?;
    if !meta.is_file() {
        return Err(FsError::InvalidPath(format!("{rel}: not a regular file")));
    }
    if meta.len() > max_bytes {
        return Err(FsError::TooLarge(meta.len()));
    }
    let mut buffer = Vec::new();
    {
        use std::io::Read;
        file.read_to_end(&mut buffer)
            .map_err(|error| FsError::Io(error.to_string()))?;
    }
    Ok(buffer)
}

/// 本内核的能力自证（23 号文档的 5.2 规则 6）。
///
/// ⚠️ **如实声明**：目前只有**只读**与**逐段 nofollow**；
/// 写/删/改名等尚未实现，不得对外声称支持。
pub fn capabilities() -> Vec<endless_kernel_core::Capability> {
    use endless_kernel_core::Capability;
    vec![
        Capability::ok("fs.open_beneath", "逐段 openat + O_NOFOLLOW，返回句柄"),
        Capability::ok("fs.read_beneath", "只读；全程只用句柄"),
        Capability::ok("fs.write_beneath", "create-or-truncate 写；逐段 nofollow"),
        Capability::ok("fs.remove_beneath", "unlinkat 删文件（不带 AT_REMOVEDIR）"),
        Capability::ok("fs.mkdir_beneath", "逐段 mkdirat + nofollow"),
        Capability::ok("fs.list_beneath", "fdopendir 列目录；不跟随 symlink"),
        Capability::ok("fs.rename_beneath", "renameat；两端逐段 nofollow"),
        Capability::missing(
            "fs.copy_beneath",
            "内核不提供独立 copy 原语；工具层用 read_beneath + write_beneath 组合实现",
        ),
    ]
}


/// 打开**父目录**目录链，返回 (父目录句柄, 末段名)。
/// 与 `open_beneath` 同样的逐段 `O_NOFOLLOW`，只是停在倒数第二段。
fn open_parent<'a>(root: &Path, segments: &[&'a str]) -> Result<(OwnedFd, &'a str), FsError> {
    let root_fd = openat(CWD, root, OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())
        .map_err(|errno| map_errno(errno, "<root>"))?;
    let last_index = segments.len() - 1;
    let mut current: OwnedFd = root_fd;
    for segment in segments.iter().take(last_index) {
        let flags = OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::DIRECTORY | OFlags::RDONLY;
        current = openat(&current, *segment, flags, Mode::empty())
            .map_err(|errno| map_errno(errno, segment))?;
    }
    Ok((current, segments[last_index]))
}

/// 覆盖写一个文件：全程句柄，逐段 nofollow。
///
/// ⚠️ 语义是 **create-or-truncate**（`O_CREAT | O_TRUNC`），不是追加。
pub fn write_beneath(root: &Path, rel: &str, data: &[u8]) -> Result<(), FsError> {
    let fd = open_beneath(root, rel, OpenMode::Create)?;
    let mut file = std::fs::File::from(fd);
    {
        use std::io::Write;
        file.write_all(data)
            .map_err(|error| FsError::Io(error.to_string()))?;
    }
    Ok(())
}

/// 删除一个**文件**（`unlinkat`，刻意**不**带 `AT_REMOVEDIR`：不删目录）。
pub fn remove_beneath(root: &Path, rel: &str) -> Result<(), FsError> {
    let segments = split_segments(rel)?;
    let (parent, name) = open_parent(root, &segments)?;
    rustix::fs::unlinkat(&parent, name, rustix::fs::AtFlags::empty())
        .map_err(|errno| map_errno(errno, name))?;
    Ok(())
}


/// 逐段建目录链（语义 = `Path.mkdir(parents=True, exist_ok=True)`），
/// **每一段都带 `O_NOFOLLOW`** —— 中途任一段是 symlink 即拒，不会在区外建目录。
///
/// 空路径（即工作区根本身）视为**已存在**，直接 Ok。
pub fn mkdir_beneath(root: &Path, rel: &str) -> Result<(), FsError> {
    if rel.is_empty() || rel == "." {
        return Ok(());
    }
    let segments = split_segments(rel)?;
    let root_fd = openat(CWD, root, OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())
        .map_err(|errno| map_errno(errno, "<root>"))?;
    let mut current: OwnedFd = root_fd;
    for segment in segments.iter() {
        let dir_flags = OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::DIRECTORY | OFlags::RDONLY;
        match openat(&current, *segment, dir_flags, Mode::empty()) {
            Ok(next) => {
                current = next;
            }
            Err(rustix::io::Errno::NOENT) => {
                // 不存在才建；已存在（EEXIST）由上层 openat 复核，不由这里兜。
                rustix::fs::mkdirat(
                    &current,
                    *segment,
                    Mode::RUSR | Mode::WUSR | Mode::XUSR,
                )
                .map_err(|errno| map_errno(errno, segment))?;
                current = openat(&current, *segment, dir_flags, Mode::empty())
                    .map_err(|errno| map_errno(errno, segment))?;
            }
            Err(errno) => return Err(map_errno(errno, segment)),
        }
    }
    Ok(())
}


/// 一个目录项。**不跟随符号链接**：is_symlink 如实标记，由调用方决定怎么处理。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirEntryInfo {
    pub name: String,
    pub is_dir: bool,
    pub is_file: bool,
    pub is_symlink: bool,
    /// lstat 语义的大小（**不跟随符号链接**：symlink 报的是链接自身长度）。
    pub size: u64,
}

/// 列目录（**通过句柄**，不重新解析路径）。空 rel = 工作区根本身。
///
/// 返回按名字排序，保证结果确定（差分/快照可比）。
pub fn list_beneath(root: &Path, rel: &str) -> Result<Vec<DirEntryInfo>, FsError> {
    let fd = if rel.is_empty() || rel == "." {
        openat(CWD, root, OFlags::DIRECTORY | OFlags::CLOEXEC, Mode::empty())
            .map_err(|errno| map_errno(errno, "<root>"))?
    } else {
        open_beneath(root, rel, OpenMode::Read)?
    };
    let dir = rustix::fs::Dir::read_from(&fd).map_err(|errno| map_errno(errno, rel))?;
    let mut out: Vec<DirEntryInfo> = Vec::new();
    for item in dir {
        let entry = item.map_err(|errno| map_errno(errno, rel))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        // readdir 会把 `.` 与 `..` 一并返回；但工具语义（Python iterdir()）不含它们。
        // 不过滤会让内核比参考实现多出两条 —— 那是等价性缺陷。
        if name == "." || name == ".." {
            continue;
        }
        let file_type = entry.file_type();
        // 大小走 lstat（AT_SYMLINK_NOFOLLOW）——**不跟随**符号链接：
        // 跟随会让"列目录"变成"读出区外目标的大小"，那正是要闭合的那条缝。
        let size = rustix::fs::statat(
            &fd,
            entry.file_name(),
            rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
        )
        .map(|stat| stat.st_size as u64)
        .unwrap_or(0);
        out.push(DirEntryInfo {
            name,
            is_dir: file_type.is_dir(),
            is_file: file_type.is_file(),
            is_symlink: file_type.is_symlink(),
            size,
        });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}


/// 改名 / 移动（工作区根内）。**两端都逐段 nofollow**：
/// 于是"把文件移到工作区外"这条路在结构上不成立。
pub fn rename_beneath(root: &Path, from_rel: &str, to_rel: &str) -> Result<(), FsError> {
    let from_segments = split_segments(from_rel)?;
    let to_segments = split_segments(to_rel)?;
    let (from_parent, from_name) = open_parent(root, &from_segments)?;
    let (to_parent, to_name) = open_parent(root, &to_segments)?;
    rustix::fs::renameat(&from_parent, from_name, &to_parent, to_name)
        .map_err(|errno| map_errno(errno, to_name))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    struct TempTree {
        root: PathBuf,
    }

    impl TempTree {
        fn new(tag: &str) -> Self {
            let mut root = std::env::temp_dir();
            root.push(format!("endless-fs-test-{}-{}", std::process::id(), tag));
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

        fn mkdir(&self, rel: &str) {
            std::fs::create_dir_all(self.root.join(rel)).expect("mkdir");
        }

        fn symlink(&self, target: &str, link: &str) {
            std::os::unix::fs::symlink(self.root.join(target), self.root.join(link))
                .expect("symlink");
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn split_segments_rejects_absolute_and_parent() {
        assert!(matches!(
            split_segments("/etc/passwd"),
            Err(FsError::InvalidPath(_))
        ));
        assert!(matches!(
            split_segments("a/../../b"),
            Err(FsError::InvalidPath(_))
        ));
        assert!(matches!(split_segments(""), Err(FsError::InvalidPath(_))));
        assert_eq!(vec!["a", "b"], split_segments("a/./b").unwrap());
    }

    #[test]
    fn reads_a_file_inside_the_root() {
        let tree = TempTree::new("read");
        tree.write("sub/file.txt", "inside");
        let data = read_beneath(&tree.root, "sub/file.txt", 1024).expect("read");
        assert_eq!(b"inside", &data[..]);
    }

    #[test]
    fn missing_file_is_reported_as_not_found() {
        let tree = TempTree::new("missing");
        let err = read_beneath(&tree.root, "nope.txt", 1024).unwrap_err();
        assert!(matches!(err, FsError::NotFound(_)), "got {err:?}");
    }

    #[test]
    fn rejects_when_the_last_segment_is_a_symlink() {
        let tree = TempTree::new("lastlink");
        tree.mkdir("outside");
        tree.write("outside/secret.txt", "OUTSIDE");
        tree.symlink("outside/secret.txt", "link.txt");

        let err = read_beneath(&tree.root, "link.txt", 1024).unwrap_err();
        match err {
            FsError::SymlinkSegment(segment) => assert_eq!("link.txt", segment),
            other => panic!("expected SymlinkSegment, got {other:?}"),
        }
    }

    /// ★ 这就是 RK-3 的验收门②：探针里那条越界读取，在内核下必须失败。
    #[test]
    fn rejects_after_an_intermediate_directory_is_swapped_to_a_symlink() {
        let tree = TempTree::new("toctou");
        tree.write("sub/file.txt", "inside");
        tree.mkdir("outside");
        tree.write("outside/file.txt", "OUTSIDE");

        // 第一次：正常读得到。
        let first = read_beneath(&tree.root, "sub/file.txt", 1024).expect("first read");
        assert_eq!(b"inside", &first[..]);

        // 把中间目录换成指向区外的 symlink —— **不需要竞态**。
        std::fs::rename(tree.root.join("sub"), tree.root.join("sub_real")).expect("rename");
        tree.symlink("outside", "sub");

        // 第二次：必须拒绝，且**绝不能**读到区外内容。
        let err = read_beneath(&tree.root, "sub/file.txt", 1024).unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "symlink swap must be rejected, got {err:?}"
        );
    }

    #[test]
    fn refuses_files_over_the_limit() {
        let tree = TempTree::new("limit");
        tree.write("big.txt", "0123456789");
        let err = read_beneath(&tree.root, "big.txt", 4).unwrap_err();
        assert!(matches!(err, FsError::TooLarge(10)), "got {err:?}");
    }

    #[test]
    fn open_beneath_returns_a_handle_not_a_path_snapshot() {
        // 拿到句柄之后，即使**名字被换到另一个 inode**，句柄仍指向原来那个。
        // ⚠️ 不能用 std::fs::write 来"替换" —— 它是在**同一个 inode** 上截断重写，
        // 句柄看到新内容属 POSIX 正常行为，不是漏洞（本用例第一版就栽在这）。
        let tree = TempTree::new("handle");
        tree.write("file.txt", "original");
        tree.write("other.txt", "replaced");

        let fd = open_beneath(&tree.root, "file.txt", OpenMode::Read).expect("open");
        // 让 file.txt 这个名字改指向 other.txt 的 inode
        std::fs::rename(tree.root.join("other.txt"), tree.root.join("file.txt"))
            .expect("rename");

        let mut file = std::fs::File::from(fd);
        let mut buffer = String::new();
        {
            use std::io::Read;
            file.read_to_string(&mut buffer).expect("read");
        }
        assert_eq!("original", buffer);
    }

    #[test]
    fn capabilities_are_reported_truthfully() {
        let caps = capabilities();
        assert!(caps.iter().any(|c| c.name == "fs.open_beneath" && c.available));
        // 未实现的能力必须**如实标为不可用**，不许声称支持。
        assert!(caps
            .iter()
            .any(|c| c.name == "fs.copy_beneath" && !c.available));
    }

    #[test]
    fn write_then_read_round_trips() {
        let tree = TempTree::new("write");
        write_beneath(&tree.root, "new.txt", b"hello").expect("write");
        let data = read_beneath(&tree.root, "new.txt", 1024).expect("read");
        assert_eq!(b"hello", &data[..]);
    }

    #[test]
    fn write_refuses_to_escape_via_symlink() {
        let tree = TempTree::new("writesymlink");
        tree.mkdir("outside");
        tree.symlink("outside", "link");

        let err = write_beneath(&tree.root, "link/pwned.txt", b"x").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
        // 区外**绝不能**出现这个文件
        assert!(!tree.root.join("outside").join("pwned.txt").exists());
    }

    #[test]
    fn remove_deletes_a_file_inside_the_root() {
        let tree = TempTree::new("remove");
        tree.write("gone.txt", "bye");
        remove_beneath(&tree.root, "gone.txt").expect("remove");
        assert!(!tree.root.join("gone.txt").exists());
    }

    #[test]
    fn remove_refuses_to_escape_via_symlink() {
        let tree = TempTree::new("removesymlink");
        tree.mkdir("outside");
        std::fs::write(tree.root.join("outside").join("victim.txt"), "keep me").expect("seed");
        tree.symlink("outside", "link");

        let err = remove_beneath(&tree.root, "link/victim.txt").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
        // 区外文件**绝不能**被删掉
        assert!(tree.root.join("outside").join("victim.txt").exists());
    }



    #[test]
    fn mkdir_creates_the_whole_chain_and_tolerates_existing() {
        let tree = TempTree::new("mkdir");
        mkdir_beneath(&tree.root, "a/b/c").expect("mkdir chain");
        assert!(tree.root.join("a").join("b").join("c").is_dir());
        // 幂等：再来一次不报错（等价 mkdir(parents=True, exist_ok=True)）
        mkdir_beneath(&tree.root, "a/b/c").expect("idempotent");
        // 空路径 = 工作区根，视为已存在
        mkdir_beneath(&tree.root, "").expect("root is fine");
    }

    #[test]
    fn mkdir_refuses_to_escape_via_symlink() {
        let tree = TempTree::new("mkdirlink");
        tree.mkdir("outside");
        tree.symlink("outside", "link");

        let err = mkdir_beneath(&tree.root, "link/created").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
        // 区外**绝不能**被建出目录
        assert!(!tree.root.join("outside").join("created").exists());
    }



    #[test]
    fn list_reports_entries_without_following_symlinks() {
        let tree = TempTree::new("list");
        tree.write("dir/inner.txt", "x");
        tree.write("plain.txt", "y");
        tree.mkdir("outside");
        tree.symlink("outside", "link");

        let entries = list_beneath(&tree.root, "").expect("list root");
        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();
        assert_eq!(vec!["dir", "link", "outside", "plain.txt"], names);

        let link = entries.iter().find(|e| e.name == "link").expect("link");
        assert!(link.is_symlink, "symlink 必须如实标记，不得跟随");
        assert!(!link.is_dir);

        let dir = entries.iter().find(|e| e.name == "dir").expect("dir");
        assert!(dir.is_dir && !dir.is_symlink);

        // 子目录也能列（逐段 nofollow）
        let inner = list_beneath(&tree.root, "dir").expect("list sub");
        assert_eq!(vec!["inner.txt"], inner.iter().map(|e| e.name.as_str()).collect::<Vec<_>>());
    }

    #[test]
    fn list_refuses_to_escape_via_symlink() {
        let tree = TempTree::new("listlink");
        tree.mkdir("outside");
        std::fs::write(tree.root.join("outside").join("secret.txt"), "S").expect("seed");
        tree.symlink("outside", "link");

        // 注意：`link` 本身能列出来（它是工作区内的一个条目），
        // 但**顺着它进去列**必须被拒。
        let err = list_beneath(&tree.root, "link").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
    }



    #[test]
    fn rename_moves_within_the_root() {
        let tree = TempTree::new("rename");
        tree.write("a.txt", "content");
        tree.mkdir("dir");
        rename_beneath(&tree.root, "a.txt", "dir/b.txt").expect("rename");
        assert!(!tree.root.join("a.txt").exists());
        assert_eq!(
            b"content",
            &read_beneath(&tree.root, "dir/b.txt", 1024).unwrap()[..]
        );
    }

    #[test]
    fn rename_refuses_to_escape_via_symlink() {
        let tree = TempTree::new("renamelink");
        tree.write("victim.txt", "secret");
        tree.mkdir("outside");
        tree.symlink("outside", "link");

        // 目标经 symlink 段 ⇒ 必拒
        let err = rename_beneath(&tree.root, "victim.txt", "link/stolen.txt").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
        // 源文件必须**仍在原位**、区外**没有**出现它
        assert!(tree.root.join("victim.txt").exists());
        assert!(!tree.root.join("outside").join("stolen.txt").exists());
    }

    #[test]
    fn rename_refuses_when_the_source_goes_through_a_symlink() {
        let tree = TempTree::new("renamesrclink");
        tree.mkdir("outside");
        std::fs::write(tree.root.join("outside").join("f.txt"), "S").expect("seed");
        tree.symlink("outside", "link");

        let err = rename_beneath(&tree.root, "link/f.txt", "moved.txt").unwrap_err();
        assert!(
            matches!(err, FsError::SymlinkSegment(_) | FsError::NotADirectory(_)),
            "got {err:?}"
        );
        assert!(tree.root.join("outside").join("f.txt").exists());
    }


}