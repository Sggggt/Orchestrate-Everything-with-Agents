use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if env::var("TARGET").unwrap().ends_with("windows-gnu") {
        // tracelogging's desktop ETW imports are exported by Advapi32. MinGW
        // supplies that import library but not the MSVC OneCore umbrella name.
        // This link-only alias changes neither SDK code nor runtime policy.
        let directory = PathBuf::from(env::var_os("OUT_DIR").unwrap());
        fs::write(directory.join("libOneCore_apiset.a"), "INPUT ( -ladvapi32 )\n").unwrap();
        println!("cargo:rustc-link-search=native={}", directory.display());
    }
}
