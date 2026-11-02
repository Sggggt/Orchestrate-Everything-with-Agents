use mxc_sdk::v1::{Containment, ContainerRequest, NetworkAction};
use serde_json::{json, Value};
use std::io;

pub type Watcher = Box<dyn FnOnce() -> io::Result<()> + Send>;

pub fn inspect() -> Value {
    #[cfg(target_os = "windows")]
    {
        match mxc_sdk::v1::probe(None) {
            Ok(facts) => json!({"name":"process_container", "supported":facts.tier == Some("base-container")
                && facts.needs_dacl_augmentation == Some(false), "facts":facts, "execution_verified":false}),
            Err(error) => json!({"name":"process_container", "supported":false, "error":error.to_string(), "execution_verified":false}),
        }
    }
    #[cfg(target_os = "linux")]
    {
        match std::process::Command::new("bwrap").arg("--version").output() {
            Ok(output) => json!({"name":"bubblewrap", "installed":output.status.success(),
                "version":String::from_utf8_lossy(&output.stdout).trim(), "execution_verified":false}),
            Err(error) => json!({"name":"bubblewrap", "installed":false, "error":error.to_string(), "execution_verified":false}),
        }
    }
}

#[cfg(target_os = "linux")]
pub fn command(argv: &[String]) -> String {
    format!("exec {}", argv.iter().map(|value| format!("'{}'", value.replace('\'', "'\\''"))).collect::<Vec<_>>().join(" "))
}

#[cfg(target_os = "windows")]
pub fn command(argv: &[String]) -> String {
    // CreateProcessW uses CRT command-line rules; no shell interprets this text.
    argv.iter().map(|value| {
        let mut quoted = String::from("\"");
        let mut slashes = 0;
        for character in value.chars() {
            if character == '\\' { slashes += 1; continue; }
            quoted.push_str(&"\\".repeat(slashes * if character == '"' { 2 } else { 1 }));
            slashes = 0;
            if character == '"' { quoted.push('\\'); }
            quoted.push(character);
        }
        quoted.push_str(&"\\".repeat(slashes * 2));
        quoted.push('"');
        quoted
    }).collect::<Vec<_>>().join(" ")
}

pub fn containment() -> Containment {
    #[cfg(target_os = "windows")]
    { Containment::ProcessContainer(Default::default()) }
    #[cfg(target_os = "linux")]
    { Containment::Bubblewrap }
}

pub fn ingress(action: NetworkAction) -> NetworkAction {
    #[cfg(target_os = "windows")]
    { action }
    #[cfg(target_os = "linux")]
    { let _ = action; NetworkAction::Deny }
}

pub fn validate(request: &ContainerRequest, detailed: bool) -> Result<Value, String> {
    #[cfg(target_os = "windows")]
    {
        let facts = mxc_sdk::v1::probe(Some(request)).map_err(|e| e.to_string())?;
        if facts.error.is_some() || facts.tier != Some("base-container") || facts.needs_dacl_augmentation != Some(false)
            || !facts.probes.base_container_api_present {
            return Err(format!("Native PSEC is required; fallback is disabled: {facts:?}"));
        }
        let mut capabilities = json!({"backend":"process_container", "isolated":true, "pty":false, "native_psec":true,
            "network_ingress":"policy", "host_loopback":"policy", "ui":"container_isolated",
            "clipboard":"deny", "input_injection":"deny"});
        if detailed { capabilities["probe"] = serde_json::to_value(facts).map_err(|e| e.to_string())?; }
        Ok(capabilities)
    }
    #[cfg(target_os = "linux")]
    {
        let _ = (request, detailed);
        let present = std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
            .any(|directory| directory.join("bwrap").is_file());
        if !present { return Err("Bubblewrap is required; no fallback is available".into()); }
        Ok(json!({"backend":"bubblewrap", "isolated":true, "pty":false,
            "network_ingress":"deny", "host_loopback":"deny"}))
    }
}

#[cfg(target_os = "linux")]
pub fn watch(pid: u32) -> io::Result<Watcher> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let descriptor = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) } as i32;
    if descriptor < 0 { return Err(io::Error::last_os_error()); }
    let descriptor = unsafe { OwnedFd::from_raw_fd(descriptor) };
    Ok(Box::new(move || {
        let mut poll = libc::pollfd { fd: descriptor.as_raw_fd(), events: libc::POLLIN, revents: 0 };
        loop {
            if unsafe { libc::poll(&mut poll, 1, -1) } >= 0 { return Ok(()); }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted { return Err(error); }
        }
    }))
}

#[cfg(target_os = "windows")]
pub fn watch(pid: u32) -> io::Result<Watcher> {
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> isize;
        fn WaitForSingleObject(handle: isize, milliseconds: u32) -> u32;
        fn CloseHandle(handle: isize) -> i32;
    }
    let handle = unsafe { OpenProcess(0x00100000, 0, pid) };
    if handle == 0 { return Err(io::Error::last_os_error()); }
    Ok(Box::new(move || {
        let status = unsafe { WaitForSingleObject(handle, u32::MAX) };
        let result = if status == 0 { Ok(()) } else { Err(io::Error::last_os_error()) };
        unsafe { CloseHandle(handle); }
        result
    }))
}
