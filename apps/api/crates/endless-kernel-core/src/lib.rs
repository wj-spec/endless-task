//! L1 内核底座：ABI 版本与能力自证。
//! 本 crate 是 L1：零 pyo3 依赖，不知道 Python 存在，cargo test 可独立全绿。
pub const KERNEL_ABI_VERSION: u32 = 1;

/// 一个能力的如实声明。available=false 时 detail 必须写明原因，供上层指名报错。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Capability {
    pub name: String,
    pub available: bool,
    pub detail: String,
}

impl Capability {
    pub fn ok(name: impl Into<String>, detail: impl Into<String>) -> Self {
        Self { name: name.into(), available: true, detail: detail.into() }
    }

    pub fn missing(name: impl Into<String>, why: impl Into<String>) -> Self {
        Self { name: name.into(), available: false, detail: why.into() }
    }
}

/// ABI 校验：不匹配即拒绝，不做兼容猜测。
pub fn check_abi(found: u32) -> Result<(), String> {
    if found == KERNEL_ABI_VERSION {
        Ok(())
    } else {
        let head = "kernel ABI mismatch: host expects ";
        let mid = ", build reports ";
        Err([head, &KERNEL_ABI_VERSION.to_string(), mid, &found.to_string()].concat())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn abi_const_is_one() {
        assert_eq!(1u32, KERNEL_ABI_VERSION);
    }

    #[test]
    fn capability_reports_truthfully() {
        let ok = Capability::ok("core.manifest", "always present");
        let bad = Capability::missing("core.nope", "not enabled in this build");
        assert!(ok.available);
        assert!(!bad.available);
        assert_eq!(bad.detail, "not enabled in this build");
    }

    #[test]
    fn abi_match_is_ok_and_mismatch_is_named() {
        assert!(check_abi(KERNEL_ABI_VERSION).is_ok());
        let err = check_abi(KERNEL_ABI_VERSION + 1).unwrap_err();
        assert!(err.contains("kernel ABI mismatch"));
        assert!(err.contains("host expects"));
    }
}
