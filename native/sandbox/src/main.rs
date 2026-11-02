//! Private host component. Business streams never carry control messages.
mod platform;

use mxc_sdk::v1::{self, ContainerRequest, FilesystemPolicy, NetworkAction,
    NetworkEgressPolicy, NetworkIngressPolicy, NetworkPolicy, WaitResult};
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

const PROTOCOL: u32 = 1;

struct ExecutionFacts { launch_accepted: bool, process_stopped: bool, exit_code: Option<i32> }

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: u32,
    operation: String,
    #[serde(default)]
    argv: Vec<String>,
    cwd: String,
    readonly: Vec<String>,
    readwrite: Vec<String>,
    #[serde(default)]
    denied: Vec<String>,
    environment: BTreeMap<String, String>,
    network: String,
    timeout_ms: u32,
}

fn emit(stream: &mut TcpStream, value: Value) -> io::Result<()> {
    serde_json::to_writer(&mut *stream, &value)?;
    stream.write_all(b"\n")
}

fn request(input: Request) -> Result<ContainerRequest, String> {
    if input.protocol != PROTOCOL || input.argv.is_empty() || input.timeout_ms == 0 {
        return Err("Invalid native request version, command, or deadline".into());
    }
    if input.argv.iter().any(|v| v.contains('\0')) {
        return Err("Arguments cannot contain NUL".into());
    }
    let allow = match input.network.as_str() {
        "allow" => NetworkAction::Allow,
        "deny" => NetworkAction::Deny,
        _ => return Err("Unknown network policy".into()),
    };
    let mut result = ContainerRequest::new(platform::command(&input.argv));
    result.containment = platform::containment();
    result.filesystem = Some(FilesystemPolicy {
        readonly_paths: input.readonly, readwrite_paths: input.readwrite,
        denied_paths: input.denied, clear_policy_on_exit: Some(true),
    });
    result.network = Some(NetworkPolicy {
        egress: Some(NetworkEgressPolicy { default: Some(allow), ..Default::default() }),
        // Linux's typed Bubblewrap backend supports isolated inbound networking.
        // Outbound allowance does not expose host loopback or inbound listeners.
        ingress: Some(NetworkIngressPolicy {
            default: Some(platform::ingress(allow)), host_loopback: Some(platform::ingress(allow)),
        }),
        ..Default::default()
    });
    result.working_directory = Some(input.cwd);
    result.environment = Some(input.environment.into_iter().collect());
    result.inherit_default_environment = Some(false);
    result.timeout_ms = Some(input.timeout_ms);
    #[cfg(target_os = "windows")]
    {
        // Desktop CLI runtimes need Win32k during DLL initialization. PSEC's
        // container UI namespace still denies external UI objects, clipboard,
        // input injection, desktop switching and system/display changes.
        result.ui = Some(v1::UiPolicy { disable: false, ..Default::default() });
    }
    Ok(result)
}

enum Event { Exited(io::Result<()>), Cancel, Disconnected }

fn copy_output(mut source: Box<dyn Read + Send>, stderr: bool) -> thread::JoinHandle<io::Result<()>> {
    thread::spawn(move || {
        let mut sink: Box<dyn Write> = if stderr { Box::new(io::stderr()) } else { Box::new(io::stdout()) };
        let mut bytes = [0u8; 16384];
        loop {
            let count = source.read(&mut bytes)?;
            if count == 0 { return Ok(()); }
            sink.write_all(&bytes[..count])?;
            sink.flush()?;
        }
    })
}

fn execute(mut control: TcpStream, input: Request, facts: &mut ExecutionFacts) -> Result<(), String> {
    let preparing = std::time::Instant::now();
    let probe_only = input.operation == "probe";
    if !probe_only && input.operation != "exec" { return Err("Unknown operation".into()); }
    let timeout = input.timeout_ms;
    let policy = request(input)?;
    let capability = platform::validate(&policy, probe_only)?;
    if probe_only {
        emit(&mut control, json!({"event":"capabilities", "protocol":PROTOCOL,
            "version":env!("CARGO_PKG_VERSION"), "sdk":"1.0.1", "capabilities":capability}))
            .map_err(|e| e.to_string())?;
        return Ok(());
    }
    let mut child = v1::spawn(policy, v1::SpawnOptions {
        telemetry: Some(v1::TelemetryConfig { enabled: Some(false) }),
    }).map_err(|e| e.to_string())?;
    facts.launch_accepted = true;
    facts.process_stopped = false;
    let launch_ms = preparing.elapsed().as_millis();
    let watcher = match platform::watch(child.id()) {
        Ok(watcher) => watcher,
        Err(error) => {
            facts.process_stopped = child.kill().and_then(|_| child.wait()).is_ok();
            return Err(error.to_string());
        }
    };
    let stdout = copy_output(child.take_stdout().ok_or("Missing stdout pipe")?, false);
    let stderr = copy_output(child.take_stderr().ok_or("Missing stderr pipe")?, true);
    if let Some(mut stdin) = child.take_stdin() {
        thread::spawn(move || { let _ = io::copy(&mut io::stdin().lock(), &mut stdin); });
    }
    let (sender, receiver) = mpsc::channel();
    let exit_sender = sender.clone();
    thread::spawn(move || { let _ = exit_sender.send(Event::Exited(watcher())); });
    let mut reader = BufReader::new(control.try_clone().map_err(|e| e.to_string())?);
    thread::spawn(move || {
        let mut line = String::new();
        while matches!(reader.read_line(&mut line), Ok(n) if n > 0) {
            if line.trim() == "terminate" { let _ = sender.send(Event::Cancel); }
            line.clear();
        }
        let _ = sender.send(Event::Disconnected);
    });
    emit(&mut control, json!({"event":"started", "pid":child.id(), "capabilities":capability,
        "launch_ms":launch_ms, "ready_ms":preparing.elapsed().as_millis()}))
        .map_err(|e| e.to_string())?;
    let reason = match receiver.recv_timeout(Duration::from_millis(timeout.into())) {
        Ok(Event::Exited(result)) => { result.map_err(|e| e.to_string())?; None },
        Ok(Event::Cancel) => { child.kill().map_err(|e| e.to_string())?; Some("cancelled") },
        Ok(Event::Disconnected) | Err(mpsc::RecvTimeoutError::Disconnected) => {
            child.kill().map_err(|e| e.to_string())?; Some("host_disconnected")
        },
        Err(mpsc::RecvTimeoutError::Timeout) => {
            child.kill_for_timeout().map_err(|e| e.to_string())?; Some("timeout")
        },
    };
    let result = child.wait().map_err(|e| e.to_string())?;
    facts.process_stopped = true;
    facts.exit_code = match &result { WaitResult::Exited(code) => Some(*code), WaitResult::TimedOut => None };
    let warnings = child.warnings();
    drop(child); // SDK tears down descendants before output pipes can finish.
    for reader in [stdout, stderr] {
        reader.join().map_err(|_| "Output reader failed")?.map_err(|e| e.to_string())?;
    }
    let (code, reason) = match result {
        WaitResult::Exited(code) => (Some(code), reason),
        WaitResult::TimedOut => (None, Some("timeout")),
    };
    emit(&mut control, json!({"event":"exited", "exit_code":code,
        "termination":reason, "process_stopped":true, "warnings":warnings})).map_err(|e| e.to_string())
}

fn main() {
    let argument = std::env::args().nth(1);
    if matches!(argument.as_deref(), Some("--version" | "--inspect")) {
        let mut metadata = json!({"protocol":PROTOCOL, "version":env!("CARGO_PKG_VERSION"),
            "sdk":"1.0.1", "os":std::env::consts::OS, "arch":std::env::consts::ARCH,
            "environment":if cfg!(target_env="msvc") { "msvc" } else { "gnu" }});
        if argument.as_deref() == Some("--inspect") { metadata["backend"] = platform::inspect(); }
        println!("{metadata}");
        return;
    }
    let result = (|| -> Result<(), String> {
        let port: u16 = std::env::var("OEA_NATIVE_PORT").map_err(|_| "Host channel required")?
            .parse().map_err(|_| "Invalid host channel")?;
        let nonce = std::env::var("OEA_NATIVE_NONCE").map_err(|_| "Host nonce required")?;
        let mut control = TcpStream::connect((std::net::Ipv4Addr::LOCALHOST, port)).map_err(|e| e.to_string())?;
        control.set_nodelay(true).map_err(|e| e.to_string())?;
        emit(&mut control, json!({"nonce":nonce, "protocol":PROTOCOL, "version":env!("CARGO_PKG_VERSION")}))
            .map_err(|e| e.to_string())?;
        let mut line = String::new();
        BufReader::new(control.try_clone().map_err(|e| e.to_string())?).read_line(&mut line).map_err(|e| e.to_string())?;
        let mut facts = ExecutionFacts { launch_accepted:false, process_stopped:true, exit_code:None };
        let result = serde_json::from_str(&line).map_err(|e| e.to_string())
            .and_then(|input| execute(control.try_clone().map_err(|e| e.to_string())?, input, &mut facts));
        if let Err(error) = &result {
            let _ = emit(&mut control, json!({"event":"error", "message":error,
                "launch_accepted":facts.launch_accepted, "process_stopped":facts.process_stopped, "exit_code":facts.exit_code}));
        }
        result
    })();
    if let Err(error) = result { eprintln!("oea-sandbox: {error}"); std::process::exit(1); }
}
