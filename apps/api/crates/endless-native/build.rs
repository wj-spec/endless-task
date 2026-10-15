// macOS 上链接 Python 扩展必须允许未定义符号：`_Py*` 由解释器在**加载时**提供，
// 不在链接期解析。maturin 会自动加这个参数；这里显式加上，好让裸 `cargo build`
// 也能产出可导入的扩展（否则 `cargo build -p endless-native` 必报
// "Undefined symbols for architecture arm64: _PyBool_Type ..."）。
fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("macos") {
        println!("cargo:rustc-link-arg=-Wl,-undefined,dynamic_lookup");
    }
}