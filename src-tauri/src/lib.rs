use serde::Serialize;
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager};

const MAX_CAPTURE_PIXELS: u64 = 40_000_000;
const MAX_CLIPBOARD_RGBA_BYTES: usize = (MAX_CAPTURE_PIXELS as usize) * 4;
const MAX_PROFILE_IMAGE_READ_BYTES: u64 = 2 * 1024 * 1024;
const PROGRESS_DISCRIMINATOR: &str = "__odysseus_progress__";
const OPERATION_PROGRESS_EVENT: &str = "operation_progress";

struct ProgressEvent {
    name: &'static str,
    payload: Value,
}

fn parse_progress_event(line: &str) -> Option<ProgressEvent> {
    if !line.contains(PROGRESS_DISCRIMINATOR) {
        return None;
    }
    let payload: Value = serde_json::from_str(line).ok()?;
    if payload.get(PROGRESS_DISCRIMINATOR).and_then(Value::as_bool) != Some(true) {
        return None;
    }
    Some(ProgressEvent {
        name: OPERATION_PROGRESS_EVENT,
        payload,
    })
}

fn read_next_rpc_response<R: BufRead>(
    reader: &mut R,
    expected_id: u64,
) -> std::io::Result<Option<Value>> {
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 {
            return Ok(None);
        }

        let Ok(response) = serde_json::from_str::<Value>(line.trim()) else {
            continue;
        };

        if response.get("id").and_then(Value::as_u64) == Some(expected_id) {
            return Ok(Some(response));
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ChildStopOutcome {
    AlreadyExited,
    ExitedDuringGracePeriod,
    KilledAfterGracePeriod,
}

impl ChildStopOutcome {
    fn log_value(self) -> &'static str {
        match self {
            Self::AlreadyExited | Self::ExitedDuringGracePeriod => "graceful",
            Self::KilledAfterGracePeriod => "forced",
        }
    }
}

fn stop_child_after_grace_period(
    child: &mut Child,
    grace_period: Duration,
) -> Result<ChildStopOutcome, String> {
    match child.try_wait() {
        Ok(Some(_status)) => return Ok(ChildStopOutcome::AlreadyExited),
        Ok(None) => {}
        Err(err) => return Err(err.to_string()),
    }

    let deadline = Instant::now() + grace_period;

    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_status)) => return Ok(ChildStopOutcome::ExitedDuringGracePeriod),
            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(err) => return Err(err.to_string()),
        }
    }

    match child.try_wait() {
        Ok(Some(_status)) => return Ok(ChildStopOutcome::ExitedDuringGracePeriod),
        Ok(None) => {}
        Err(err) => return Err(err.to_string()),
    }

    if let Err(kill_error) = child.kill() {
        return match child.try_wait() {
            Ok(Some(_status)) => Ok(ChildStopOutcome::KilledAfterGracePeriod),
            _ => Err(kill_error.to_string()),
        };
    }
    child.wait().map_err(|err| err.to_string())?;
    Ok(ChildStopOutcome::KilledAfterGracePeriod)
}

fn write_shutdown_request<W: Write>(writer: &mut W, request_id: u64) -> Result<(), String> {
    let request = json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "app.shutdown",
        "params": {},
    });
    writeln!(writer, "{}", request).map_err(|err| err.to_string())?;
    writer.flush().map_err(|err| err.to_string())
}

#[derive(Serialize)]
struct AppStatus {
    profile_id: String,
    profile_dir: String,
    backend_ready: bool,
}

struct AppState {
    profile_id: String,
    profile_dir: PathBuf,
    backend: Mutex<BackendClient>,
}

struct SidecarLaunchContext {
    resource_dir: PathBuf,
    app_data_dir: PathBuf,
    dev_repo_root: Option<PathBuf>,
}

const PROXY_ENV_VARS: &[&str] = &[
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
];

/// Removes proxy env vars the sidecar would otherwise inherit from the parent
/// process, since `urllib.request` honors them automatically for any HTTP
/// call the Python backend makes.
fn strip_proxy_env_vars(command: &mut Command) {
    for name in PROXY_ENV_VARS {
        command.env_remove(name);
    }
}

impl Drop for AppState {
    fn drop(&mut self) {
        if let Ok(mut backend) = self.backend.lock() {
            let _ = backend.shutdown(&self.profile_dir);
        }
    }
}

struct BackendClient {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<std::process::ChildStdout>,
    next_id: u64,
    is_shutdown: bool,
}

struct BackendCallError {
    message: String,
    restartable: bool,
    lifecycle_event: Option<&'static str>,
}

impl BackendCallError {
    fn restartable(message: String, lifecycle_event: &'static str) -> Self {
        Self {
            message,
            restartable: true,
            lifecycle_event: Some(lifecycle_event),
        }
    }

    fn final_error(message: String) -> Self {
        Self {
            message,
            restartable: false,
            lifecycle_event: None,
        }
    }
}

impl BackendClient {
    fn spawn(app: &AppHandle, profile_dir: &Path) -> Result<Self, String> {
        let python = locate_python(app)?;
        let script = locate_sidecar_script(app)?;
        let launch = sidecar_launch_context(app, profile_dir)?;
        let florence_model_dir = std::env::var("ODYSSEUS_FLORENCE_MODEL_DIR").ok();

        append_shell_log(
            profile_dir,
            &format!(
                "sidecar launch executable={} script={} cwd={} resource_dir={} dev_repo_root={} app_data_dir={} florence_model_dir={}",
                python.display(),
                script.display(),
                launch.resource_dir.display(),
                launch.resource_dir.display(),
                launch
                    .dev_repo_root
                    .as_ref()
                    .map(|path| path.display().to_string())
                    .unwrap_or_else(|| "unset".to_string()),
                launch.app_data_dir.display(),
                florence_model_dir.as_deref().unwrap_or("unset")
            ),
        );

        let mut command = Command::new(&python);
        command
            .arg("-u")
            .arg(&script)
            .current_dir(&launch.resource_dir)
            .env("ODYSSEUS_PROFILE_DIR", profile_dir)
            .env("ODYSSEUS_RESOURCE_DIR", &launch.resource_dir)
            .env("ODYSSEUS_APP_DATA_DIR", &launch.app_data_dir)
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8");
        command
            .env("HF_HUB_OFFLINE", "1")
            .env("TRANSFORMERS_OFFLINE", "1");
        if let Some(dev_repo_root) = &launch.dev_repo_root {
            command.env("ODYSSEUS_DEV_REPO_ROOT", dev_repo_root);
        }
        if let Some(model_dir) = &florence_model_dir {
            command.env("ODYSSEUS_FLORENCE_MODEL_DIR", model_dir);
        }
        strip_proxy_env_vars(&mut command);

        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|err| format!("failed to start Python sidecar with {:?}: {}", python, err))?;

        if let Some(stderr) = child.stderr.take() {
            let progress_app = app.clone();
            thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines().flatten() {
                    if line.contains(PROGRESS_DISCRIMINATOR) {
                        if let Some(event) = parse_progress_event(&line) {
                            let _ = progress_app.emit(event.name, event.payload);
                        }
                        continue;
                    }
                    eprintln!("[python-sidecar] {line}");
                }
            });
        }

        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| "Python sidecar stdin was not available".to_string())?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| "Python sidecar stdout was not available".to_string())?;

        let mut client = Self {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 1,
            is_shutdown: false,
        };

        client
            .call_once("health.ping", json!({}))
            .map_err(|err| err.message)?;
        Ok(client)
    }

    fn call_with_recovery(
        &mut self,
        app: &AppHandle,
        profile_dir: &Path,
        method: &str,
        params: Value,
    ) -> Result<Value, String> {
        self.call_with_recovery_using(profile_dir, method, params, |client| {
            client.restart(app, profile_dir)
        })
    }

    fn call_with_recovery_using<F>(
        &mut self,
        profile_dir: &Path,
        method: &str,
        params: Value,
        restart: F,
    ) -> Result<Value, String>
    where
        F: FnOnce(&mut Self) -> Result<(), String>,
    {
        match self.call_once(method, params.clone()) {
            Ok(value) => Ok(value),
            Err(err) if err.restartable => {
                append_shell_log(
                    profile_dir,
                    &format!(
                        "sidecar lifecycle phase=exit result=detected event={}",
                        err.lifecycle_event.unwrap_or("unknown")
                    ),
                );
                if !can_restart_and_retry(method) {
                    append_shell_log(
                        profile_dir,
                        "sidecar lifecycle phase=restart result=skipped_non_idempotent",
                    );
                    return Err(err.message);
                }

                append_shell_log(
                    profile_dir,
                    "sidecar lifecycle phase=restart result=attempted",
                );
                if let Err(restart_error) = restart(self) {
                    append_shell_log(
                        profile_dir,
                        "sidecar lifecycle phase=restart result=failed",
                    );
                    return Err(format!("Python sidecar restart failed: {restart_error}"));
                }
                append_shell_log(
                    profile_dir,
                    "sidecar lifecycle phase=restart result=succeeded",
                );

                match self.call_once(method, params) {
                    Ok(value) => {
                        append_shell_log(
                            profile_dir,
                            "sidecar lifecycle phase=retry result=succeeded",
                        );
                        Ok(value)
                    }
                    Err(retry_error) => {
                        append_shell_log(
                            profile_dir,
                            "sidecar lifecycle phase=retry result=failed",
                        );
                        Err(retry_error.message)
                    }
                }
            }
            Err(err) => Err(err.message),
        }
    }

    fn call_once(&mut self, method: &str, params: Value) -> Result<Value, BackendCallError> {
        if self.is_shutdown {
            return Err(BackendCallError::restartable(
                "Python sidecar is not running".to_string(),
                "not_running",
            ));
        }

        match self.child.try_wait() {
            Ok(Some(status)) => {
                self.is_shutdown = true;
                return Err(BackendCallError::restartable(
                    format!("Python sidecar exited before handling {method} (status: {status})"),
                    "child_exited",
                ));
            }
            Ok(None) => {}
            Err(err) => {
                return Err(BackendCallError::final_error(format!(
                    "failed to inspect Python sidecar before {method}: {err}"
                )));
            }
        }

        let id = self.next_id;
        self.next_id += 1;

        let request = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method,
            "params": params,
        });

        if let Err(err) = writeln!(self.stdin, "{}", request) {
            self.is_shutdown = true;
            return Err(BackendCallError::restartable(
                format!("Python sidecar pipe closed while sending {method}: {err}"),
                "stdin_closed",
            ));
        }

        if let Err(err) = self.stdin.flush() {
            self.is_shutdown = true;
            return Err(BackendCallError::restartable(
                format!("Python sidecar pipe closed while flushing {method}: {err}"),
                "stdin_closed",
            ));
        }

        let response = match read_next_rpc_response(&mut self.stdout, id).map_err(|err| {
            BackendCallError::restartable(
                format!("failed to read Python sidecar response for {method}: {err}"),
                "stdout_closed",
            )
        })? {
            Some(response) => response,
            None => {
                self.is_shutdown = true;
                let message = format!("Python sidecar exited before responding to {method}");
                return Err(BackendCallError::restartable(message, "stdout_closed"));
            }
        };

        if let Some(error) = response.get("error") {
            let message = error
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("sidecar error");

            return Err(BackendCallError::final_error(message.to_string()));
        }

        Ok(response.get("result").cloned().unwrap_or(Value::Null))
    }

    fn restart(&mut self, app: &AppHandle, profile_dir: &Path) -> Result<(), String> {
        let _ = self.shutdown(profile_dir);
        *self = Self::spawn(app, profile_dir)?;
        Ok(())
    }

    fn is_ready(&mut self) -> bool {
        if self.is_shutdown {
            return false;
        }

        match self.child.try_wait() {
            Ok(Some(_status)) => {
                self.is_shutdown = true;
                false
            }
            Ok(None) => true,
            Err(_err) => false,
        }
    }

    fn request_shutdown_without_waiting(&mut self) -> Result<&'static str, String> {
        match self.child.try_wait() {
            Ok(Some(_status)) => return Ok("already_exited"),
            Ok(None) => {}
            Err(err) => return Err(err.to_string()),
        }

        let id = self.next_id;
        self.next_id += 1;
        write_shutdown_request(&mut self.stdin, id)?;
        Ok("sent")
    }

    fn shutdown(&mut self, profile_dir: &Path) -> Result<(), String> {
        self.shutdown_with_grace_period(profile_dir, Duration::from_secs(3))
    }

    fn shutdown_with_grace_period(
        &mut self,
        profile_dir: &Path,
        grace_period: Duration,
    ) -> Result<(), String> {
        if !self.is_shutdown {
            let request_result = self.request_shutdown_without_waiting();
            append_shell_log(
                profile_dir,
                &format!(
                    "sidecar shutdown phase=request result={}",
                    request_result.as_ref().copied().unwrap_or("failed")
                ),
            );
            self.is_shutdown = true;
        } else {
            append_shell_log(
                profile_dir,
                "sidecar shutdown phase=request result=skipped_already_shutdown",
            );
        }

        let cleanup_started = Instant::now();
        match stop_child_after_grace_period(&mut self.child, grace_period) {
            Ok(outcome) => {
                append_shell_log(
                    profile_dir,
                    &format!(
                        "sidecar shutdown phase=cleanup result={} elapsed_ms={}",
                        outcome.log_value(),
                        cleanup_started.elapsed().as_millis()
                    ),
                );
                Ok(())
            }
            Err(err) => {
                append_shell_log(
                    profile_dir,
                    &format!(
                        "sidecar shutdown phase=cleanup result=failed elapsed_ms={}",
                        cleanup_started.elapsed().as_millis()
                    ),
                );
                Err(err)
            }
        }
    }
}

#[derive(Serialize)]
struct CaptureResult {
    path: String,
    source_kind: String,
    width: u32,
    height: u32,
    bytes: u64,
}

fn can_restart_and_retry(method: &str) -> bool {
    matches!(
        method,
        "diagnostics.get"
            | "models.detect_ollama"
            | "models.list"
            | "ocr.status"
            | "rag.health"
            | "evals.list"
            | "evals.history"
            | "evals.comparison"
            | "campaigns.models"
            | "campaigns.plan"
            | "campaigns.list"
            | "campaigns.get"
            | "campaigns.report_data"
    )
}

#[tauri::command]
fn capture_capabilities() -> Value {
    json!({
        "platform": std::env::consts::OS,
        "full_screen": cfg!(windows),
        "region": cfg!(windows),
        "window": false,
        "clipboard_image": true,
        "message": if cfg!(windows) {
            "Full-screen and coordinate region capture are available after an explicit user action. Window capture remains unsupported in v0.2.0."
        } else {
            "Screenshot capture is unsupported on this platform in v0.2.0."
        }
    })
}

#[tauri::command]
fn clipboard_import_image(state: tauri::State<AppState>) -> Result<CaptureResult, String> {
    let mut clipboard = arboard::Clipboard::new().map_err(|err| format!("clipboard unavailable: {err}"))?;
    let image = clipboard
        .get_image()
        .map_err(|err| format!("clipboard did not contain an image: {err}"))?;
    let width = image.width as u32;
    let height = image.height as u32;
    let bytes = image.bytes.into_owned();
    validate_capture_bounds(width, height, bytes.len())?;
    let path = capture_output_path(&state.profile_dir, "clipboard")?;
    save_rgba_png(&path, width, height, bytes)?;
    capture_result(path, "clipboard", width, height)
}

#[tauri::command]
fn capture_full_screen(state: tauri::State<AppState>, screen_index: Option<usize>) -> Result<CaptureResult, String> {
    #[cfg(not(windows))]
    {
        let _ = state;
        let _ = screen_index;
        Err("full-screen capture is unsupported on this platform in v0.2.0".to_string())
    }
    #[cfg(windows)]
    {
        let screens = screenshots::Screen::all().map_err(|err| format!("screen enumeration failed: {err}"))?;
        if screens.is_empty() {
            return Err("no screens were available for capture".to_string());
        }
        let index = screen_index.unwrap_or(0).min(screens.len() - 1);
        let image = screens[index]
            .capture()
            .map_err(|err| format!("screen capture failed: {err}"))?;
        let width = image.width();
        let height = image.height();
        validate_capture_bounds(width, height, 0)?;
        let path = capture_output_path(&state.profile_dir, "full-screen")?;
        image
            .save(&path)
            .map_err(|err| format!("failed to save screen capture: {err}"))?;
        capture_result(path, "screenshot_full", width, height)
    }
}

#[tauri::command]
fn capture_region(
    state: tauri::State<AppState>,
    x: i32,
    y: i32,
    width: u32,
    height: u32,
    screen_index: Option<usize>,
) -> Result<CaptureResult, String> {
    #[cfg(not(windows))]
    {
        let _ = state;
        let _ = (x, y, width, height, screen_index);
        Err("region capture is unsupported on this platform in v0.2.0".to_string())
    }
    #[cfg(windows)]
    {
        if width == 0 || height == 0 {
            return Err("region width and height must be positive".to_string());
        }
        validate_capture_bounds(width, height, 0)?;
        let screens = screenshots::Screen::all().map_err(|err| format!("screen enumeration failed: {err}"))?;
        if screens.is_empty() {
            return Err("no screens were available for capture".to_string());
        }
        let index = screen_index.unwrap_or(0).min(screens.len() - 1);
        let image = screens[index]
            .capture_area(x, y, width, height)
            .map_err(|err| format!("region capture failed: {err}"))?;
        let path = capture_output_path(&state.profile_dir, "region")?;
        image
            .save(&path)
            .map_err(|err| format!("failed to save region capture: {err}"))?;
        capture_result(path, "screenshot_region", width, height)
    }
}

#[tauri::command]
fn capture_window() -> Result<CaptureResult, String> {
    Err("window capture is not implemented in v0.2.0; use full-screen or region capture instead".to_string())
}

#[tauri::command]
fn read_profile_artifact_file(state: tauri::State<AppState>, path: String) -> Result<Vec<u8>, String> {
    let requested = PathBuf::from(path);
    let profile_artifacts = state
        .profile_dir
        .join("files")
        .join("artifacts")
        .canonicalize()
        .map_err(|err| format!("failed to resolve artifact directory: {err}"))?;
    let resolved = requested
        .canonicalize()
        .map_err(|err| format!("failed to resolve artifact file: {err}"))?;
    if !resolved.starts_with(&profile_artifacts) {
        return Err("artifact file read escaped the profile artifact directory".to_string());
    }
    let metadata = fs::metadata(&resolved).map_err(|err| format!("failed to inspect artifact file: {err}"))?;
    if !metadata.is_file() {
        return Err("artifact path is not a file".to_string());
    }
    if metadata.len() > MAX_PROFILE_IMAGE_READ_BYTES {
        return Err("artifact preview exceeds the secure read size limit".to_string());
    }
    fs::read(&resolved).map_err(|err| format!("failed to read artifact preview: {err}"))
}

#[tauri::command]
fn app_status(state: tauri::State<AppState>) -> AppStatus {
    let backend_ready = state
        .backend
        .lock()
        .map(|mut backend| backend.is_ready())
        .unwrap_or(false);

    AppStatus {
        profile_id: state.profile_id.clone(),
        profile_dir: state.profile_dir.display().to_string(),
        backend_ready,
    }
}

#[tauri::command]
async fn rpc_call(
    method: String,
    params: Option<Value>,
    app: AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<Value, String> {
    let mut backend = state
        .backend
        .lock()
        .map_err(|_| "backend lock is poisoned".to_string())?;

    backend.call_with_recovery(
        &app,
        &state.profile_dir,
        &method,
        params.unwrap_or_else(|| json!({})),
    )
}

#[tauri::command]
fn shutdown_backend(state: tauri::State<AppState>) -> Result<(), String> {
    let mut backend = state
        .backend
        .lock()
        .map_err(|_| "backend lock is poisoned".to_string())?;

    backend.shutdown(&state.profile_dir)
}

fn append_shell_log(profile_dir: &Path, message: &str) {
    let logs_dir = profile_dir.join("logs");
    let _ = fs::create_dir_all(&logs_dir);
    let log_path = logs_dir.join("backend.log");
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();

    if let Ok(mut file) = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
    {
        let _ = writeln!(file, "{timestamp} WARN odysseus_desktop.shell - {message}");
    }
}

fn capture_output_path(profile_dir: &Path, prefix: &str) -> Result<PathBuf, String> {
    let dir = profile_dir
        .join("files")
        .join("artifacts")
        .join("captures");
    fs::create_dir_all(&dir).map_err(|err| format!("failed to create capture dir: {err}"))?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default();
    Ok(dir.join(format!("{prefix}-{stamp}.png")))
}

fn validate_capture_bounds(width: u32, height: u32, raw_bytes: usize) -> Result<(), String> {
    let pixels = u64::from(width) * u64::from(height);
    if pixels == 0 {
        return Err("captured image dimensions must be positive".to_string());
    }
    if pixels > MAX_CAPTURE_PIXELS {
        return Err(format!("captured image exceeds {MAX_CAPTURE_PIXELS} pixel limit"));
    }
    if raw_bytes > MAX_CLIPBOARD_RGBA_BYTES {
        return Err("clipboard image exceeds the bounded capture byte limit".to_string());
    }
    Ok(())
}

fn capture_result(path: PathBuf, source_kind: &str, width: u32, height: u32) -> Result<CaptureResult, String> {
    let bytes = fs::metadata(&path)
        .map_err(|err| format!("failed to inspect capture file: {err}"))?
        .len();
    Ok(CaptureResult {
        path: path.display().to_string(),
        source_kind: source_kind.to_string(),
        width,
        height,
        bytes,
    })
}

fn save_rgba_png(path: &Path, width: u32, height: u32, bytes: Vec<u8>) -> Result<(), String> {
    let expected = width as usize * height as usize * 4;
    if bytes.len() != expected {
        return Err(format!("clipboard image byte length mismatch: expected {expected}, got {}", bytes.len()));
    }
    let buffer = image::ImageBuffer::<image::Rgba<u8>, Vec<u8>>::from_raw(width, height, bytes)
        .ok_or_else(|| "failed to create image buffer from clipboard bytes".to_string())?;
    buffer
        .save(path)
        .map_err(|err| format!("failed to save clipboard image: {err}"))
}

fn ensure_default_profile(app: &AppHandle) -> Result<(String, PathBuf), String> {
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|err| format!("failed to locate app data dir: {err}"))?;

    let profile_id = "default".to_string();
    let profile_dir = app_data.join("profiles").join(&profile_id);

    fs::create_dir_all(profile_dir.join("logs"))
        .map_err(|err| format!("failed to create profile dir: {err}"))?;

    let profile_json = profile_dir.join("profile.json");

    if !profile_json.exists() {
        fs::write(
            &profile_json,
            json!({
                "id": profile_id,
                "name": "Default",
                "schema_version": 1
            })
            .to_string(),
        )
        .map_err(|err| format!("failed to write profile metadata: {err}"))?;
    }

    Ok((profile_id, profile_dir))
}

fn sidecar_launch_context(app: &AppHandle, _profile_dir: &Path) -> Result<SidecarLaunchContext, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|err| format!("failed to locate app resource dir: {err}"))?;
    let app_data_dir = app
        .path()
        .app_data_dir()
        .map_err(|err| format!("failed to locate app data dir: {err}"))?;
    Ok(SidecarLaunchContext {
        resource_dir,
        app_data_dir,
        dev_repo_root: debug_repo_root_from_manifest_dir(Path::new(env!("CARGO_MANIFEST_DIR"))),
    })
}

fn debug_repo_root_from_manifest_dir(manifest_dir: &Path) -> Option<PathBuf> {
    if !cfg!(debug_assertions) {
        return None;
    }
    manifest_dir.parent().map(Path::to_path_buf)
}

fn locate_python(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Ok(path) = std::env::var("ODYSSEUS_PYTHON") {
        let candidate = PathBuf::from(path);

        if candidate.exists() {
            return Ok(candidate);
        }
    }

    if let Ok(resource_dir) = app.path().resource_dir() {
        let embedded = resource_dir.join("python-runtime").join(if cfg!(windows) {
            "python.exe"
        } else {
            "bin/python3"
        });

        if embedded.exists() {
            return Ok(embedded);
        }
    }

    if cfg!(debug_assertions) {
        Ok(PathBuf::from(if cfg!(windows) {
            "python"
        } else {
            "python3"
        }))
    } else {
        Err("bundled python-runtime was not found in application resources".to_string())
    }
}

fn locate_sidecar_script(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_script = app
        .path()
        .resource_dir()
        .map_err(|err| err.to_string())?
        .join("python")
        .join("rpc_server.py");

    if resource_script.exists() {
        return Ok(resource_script);
    }

    #[cfg(debug_assertions)]
    {
        let dev_script = std::env::current_dir()
            .map_err(|err| err.to_string())?
            .join("python")
            .join("rpc_server.py");

        if dev_script.exists() {
            return Ok(dev_script);
        }
    }

    Err("could not locate python/rpc_server.py".to_string())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let app_handle = app.handle().clone();
            let (profile_id, profile_dir) = ensure_default_profile(&app_handle)?;
            let backend = BackendClient::spawn(&app_handle, &profile_dir)?;

            app.manage(AppState {
                profile_id,
                profile_dir,
                backend: Mutex::new(backend),
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            app_status,
            capture_capabilities,
            clipboard_import_image,
            capture_full_screen,
            capture_region,
            capture_window,
            read_profile_artifact_file,
            rpc_call,
            shutdown_backend
        ])
        .run(tauri::generate_context!())
        .expect("error while running Odysseus Desktop");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn lifecycle_test_profile(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after Unix epoch")
            .as_nanos();
        std::env::temp_dir().join(format!("odysseus-{name}-{}-{nonce}", std::process::id()))
    }

    fn spawn_lifecycle_backend(fixture: &str) -> BackendClient {
        let current_test_binary = std::env::current_exe().expect("current test executable");
        let mut child = Command::new(current_test_binary)
            .args(["--exact", fixture, "--ignored", "--nocapture"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("lifecycle fixture should start");
        let stdin = child.stdin.take().expect("fixture stdin");
        let stdout = child.stdout.take().expect("fixture stdout");
        BackendClient {
            child,
            stdin,
            stdout: BufReader::new(stdout),
            next_id: 1,
            is_shutdown: false,
        }
    }

    fn kill_lifecycle_child(backend: &mut BackendClient) {
        backend.child.kill().expect("fixture child should be killable");
        backend.child.wait().expect("fixture child should be reaped");
    }

    #[test]
    fn normal_sidecar_stderr_is_not_a_progress_event() {
        assert!(parse_progress_event("ordinary Python log line").is_none());
    }

    #[test]
    fn valid_progress_json_maps_to_operation_progress() {
        let event = parse_progress_event(
            r#"{"__odysseus_progress__":true,"stage":"rag_search","label":"Searching sources..."}"#,
        )
        .expect("valid progress event");
        assert_eq!(event.name, "operation_progress");
        assert_eq!(event.payload["stage"], "rag_search");
    }

    #[test]
    fn malformed_progress_json_is_ignored() {
        assert!(parse_progress_event("{\"__odysseus_progress__\":true").is_none());
        assert!(parse_progress_event(r#"{"__odysseus_progress__":false}"#).is_none());
    }

    #[test]
    fn rpc_reader_skips_garbage_and_unrelated_json_before_matching_response() {
        let input = concat!(
            "PRIVATE_GARBAGE_MUST_NOT_SURFACE\n",
            r#"{"jsonrpc":"2.0","id":9,"result":"unrelated"}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":7,"result":{"ready":true}}"#,
            "\n",
        );
        let mut reader = Cursor::new(input.as_bytes());

        let response = read_next_rpc_response(&mut reader, 7)
            .expect("reader should not fail")
            .expect("matching response should be found");

        assert_eq!(response["result"]["ready"], true);
    }

    #[test]
    fn rpc_reader_skips_progress_before_matching_response() {
        let input = concat!(
            r#"{"__odysseus_progress__":true,"stage":"rag_search","label":"Searching sources..."}"#,
            "\n",
            r#"{"jsonrpc":"2.0","id":3,"result":"done"}"#,
            "\n",
        );
        let mut reader = Cursor::new(input.as_bytes());

        let response = read_next_rpc_response(&mut reader, 3)
            .expect("reader should not fail")
            .expect("matching response should be found");

        assert_eq!(response["result"], "done");
    }

    #[test]
    fn rpc_reader_ignores_malformed_progress_without_panicking() {
        let input = concat!(
            "{\"__odysseus_progress__\":true\n",
            r#"{"jsonrpc":"2.0","id":4,"result":null}"#,
            "\n",
        );
        let mut reader = Cursor::new(input.as_bytes());

        let response = read_next_rpc_response(&mut reader, 4)
            .expect("reader should not fail")
            .expect("matching response should be found");

        assert!(response["result"].is_null());
    }

    #[test]
    fn rpc_reader_returns_none_at_eof_without_a_matching_response() {
        let mut reader = Cursor::new(b"not protocol output\n".as_slice());

        assert!(read_next_rpc_response(&mut reader, 1)
            .expect("reader should not fail")
            .is_none());
    }

    #[test]
    fn shutdown_request_contains_only_fixed_protocol_metadata() {
        let private_sentinel = "PRIVATE_SHUTDOWN_SENTINEL_MUST_NOT_APPEAR";
        let mut output = Vec::new();
        write_shutdown_request(&mut output, 17).expect("shutdown request should serialize");
        let text = String::from_utf8(output).expect("shutdown request should be UTF-8");
        let request: Value = serde_json::from_str(text.trim()).expect("valid shutdown JSON");

        assert_eq!(request["jsonrpc"], "2.0");
        assert_eq!(request["id"], 17);
        assert_eq!(request["method"], "app.shutdown");
        assert_eq!(request["params"], json!({}));
        assert!(!text.contains(private_sentinel));
    }

    #[test]
    fn responsive_shutdown_is_bounded_and_repeated_shutdown_is_harmless() {
        let profile_dir = lifecycle_test_profile("responsive-shutdown");
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_shutdown_responsive_fixture");
        let started = Instant::now();

        backend
            .shutdown_with_grace_period(&profile_dir, Duration::from_secs(1))
            .expect("responsive child should shut down");
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(backend.child.try_wait().expect("child status").is_some());

        backend
            .shutdown_with_grace_period(&profile_dir, Duration::from_millis(10))
            .expect("repeated shutdown should be harmless");
        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains("sidecar shutdown phase=request result=sent"));
        assert!(log.contains("sidecar shutdown phase=cleanup result=graceful"));
        assert!(log.contains("sidecar shutdown phase=request result=skipped_already_shutdown"));
        assert!(!log.contains("PRIVATE_SHUTDOWN_SENTINEL_MUST_NOT_APPEAR"));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn hung_shutdown_is_killed_and_reaped_within_the_bound() {
        let profile_dir = lifecycle_test_profile("hung-shutdown");
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_sleeping_child_fixture");
        let started = Instant::now();

        backend
            .shutdown_with_grace_period(&profile_dir, Duration::from_millis(75))
            .expect("hung child should be killed and reaped");
        let elapsed = started.elapsed();
        assert!(elapsed < Duration::from_secs(2), "elapsed={elapsed:?}");
        assert!(backend.child.try_wait().expect("child status").is_some());

        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains("sidecar shutdown phase=request result=sent"));
        assert!(log.contains("sidecar shutdown phase=cleanup result=forced"));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn already_exited_child_cleanup_succeeds() {
        let profile_dir = lifecycle_test_profile("already-exited-shutdown");
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_already_exited_fixture");
        thread::sleep(Duration::from_millis(100));

        backend
            .shutdown_with_grace_period(&profile_dir, Duration::from_millis(10))
            .expect("already-exited child cleanup should succeed");
        assert!(backend.child.try_wait().expect("child status").is_some());

        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains("sidecar shutdown phase=request result=already_exited"));
        assert!(log.contains("sidecar shutdown phase=cleanup result=graceful"));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn forced_sidecar_death_restarts_and_retries_one_safe_request() {
        let profile_dir = lifecycle_test_profile("forced-death-safe-retry");
        let private_sentinel = "PRIVATE_SAFE_RETRY_SENTINEL_MUST_NOT_REACH_LOGS";
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_sleeping_child_fixture");
        kill_lifecycle_child(&mut backend);
        let mut restart_count = 0;

        let result = backend
            .call_with_recovery_using(
                &profile_dir,
                "diagnostics.get",
                json!({"private": private_sentinel}),
                |client| {
                    restart_count += 1;
                    *client = spawn_lifecycle_backend(
                        "tests::lifecycle_single_rpc_response_fixture",
                    );
                    Ok(())
                },
            )
            .expect("safe request should recover");

        assert_eq!(result["recovered"], true);
        assert_eq!(restart_count, 1);
        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains(
            "sidecar lifecycle phase=exit result=detected event=child_exited"
        ));
        assert!(log.contains("sidecar lifecycle phase=restart result=attempted"));
        assert!(log.contains("sidecar lifecycle phase=restart result=succeeded"));
        assert!(log.contains("sidecar lifecycle phase=retry result=succeeded"));
        assert!(!log.contains(private_sentinel));
        let _ = backend.shutdown(&profile_dir);
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn forced_sidecar_death_never_replays_non_idempotent_request() {
        let profile_dir = lifecycle_test_profile("forced-death-unsafe-request");
        let private_sentinel = "PRIVATE_UNSAFE_RETRY_SENTINEL_MUST_NOT_REACH_LOGS";
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_sleeping_child_fixture");
        kill_lifecycle_child(&mut backend);
        let mut restart_count = 0;

        let error = backend
            .call_with_recovery_using(
                &profile_dir,
                "chat.send",
                json!({"message": private_sentinel}),
                |_client| {
                    restart_count += 1;
                    Ok(())
                },
            )
            .expect_err("non-idempotent request must not be replayed");

        assert!(error.contains("exited before handling"));
        assert_eq!(restart_count, 0);
        assert!(!can_restart_and_retry("chat.send"));
        assert!(!can_restart_and_retry("evals.run"));
        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains(
            "sidecar lifecycle phase=restart result=skipped_non_idempotent"
        ));
        assert!(!log.contains(private_sentinel));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn forced_sidecar_restart_failure_is_returned_and_privacy_safe() {
        let profile_dir = lifecycle_test_profile("forced-death-restart-failure");
        let private_sentinel = "PRIVATE_RESTART_FAILURE_MUST_NOT_REACH_LOGS";
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_sleeping_child_fixture");
        kill_lifecycle_child(&mut backend);
        let mut restart_count = 0;

        let error = backend
            .call_with_recovery_using(
                &profile_dir,
                "diagnostics.get",
                json!({}),
                |_client| {
                    restart_count += 1;
                    Err(private_sentinel.to_string())
                },
            )
            .expect_err("restart failure should be returned");

        assert!(error.contains("Python sidecar restart failed"));
        assert_eq!(restart_count, 1);
        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert!(log.contains("sidecar lifecycle phase=restart result=attempted"));
        assert!(log.contains("sidecar lifecycle phase=restart result=failed"));
        assert!(!log.contains(private_sentinel));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn second_failure_after_restart_does_not_loop() {
        let profile_dir = lifecycle_test_profile("forced-death-retry-bound");
        let mut backend = spawn_lifecycle_backend("tests::lifecycle_sleeping_child_fixture");
        kill_lifecycle_child(&mut backend);
        let mut restart_count = 0;

        let error = backend
            .call_with_recovery_using(
                &profile_dir,
                "diagnostics.get",
                json!({}),
                |client| {
                    restart_count += 1;
                    let mut replacement = spawn_lifecycle_backend(
                        "tests::lifecycle_already_exited_fixture",
                    );
                    replacement
                        .child
                        .wait()
                        .expect("replacement fixture should exit");
                    *client = replacement;
                    Ok(())
                },
            )
            .expect_err("second failure should be returned without another restart");

        assert!(error.contains("exited before handling"));
        assert_eq!(restart_count, 1);
        let log = fs::read_to_string(profile_dir.join("logs").join("backend.log"))
            .expect("lifecycle log");
        assert_eq!(
            log.matches("sidecar lifecycle phase=restart result=attempted")
                .count(),
            1
        );
        assert!(log.contains("sidecar lifecycle phase=retry result=failed"));
        let _ = fs::remove_dir_all(profile_dir);
    }

    #[test]
    fn child_cleanup_kills_and_reaps_a_running_child_without_panicking() {
        let current_test_binary = std::env::current_exe().expect("current test executable");
        let mut child = Command::new(current_test_binary)
            .args([
                "--exact",
                "tests::lifecycle_sleeping_child_fixture",
                "--ignored",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("sleeping child should start");
        thread::sleep(Duration::from_millis(25));
        assert!(child
            .try_wait()
            .expect("child status should be inspectable")
            .is_none());

        let outcome = stop_child_after_grace_period(&mut child, Duration::from_millis(50))
            .expect("cleanup should kill and reap the child");
        assert_eq!(outcome, ChildStopOutcome::KilledAfterGracePeriod);

        assert!(child
            .try_wait()
            .expect("child status should remain inspectable")
            .is_some());
    }

    #[test]
    #[ignore = "helper process spawned by child_cleanup_kills_and_reaps_a_running_child_without_panicking"]
    fn lifecycle_sleeping_child_fixture() {
        thread::sleep(Duration::from_secs(60));
    }

    #[test]
    #[ignore = "helper process spawned by responsive shutdown test"]
    fn lifecycle_shutdown_responsive_fixture() {
        let mut request = String::new();
        std::io::stdin()
            .read_line(&mut request)
            .expect("shutdown request");
        let request: Value = serde_json::from_str(request.trim()).expect("valid shutdown request");
        assert_eq!(request["method"], "app.shutdown");
    }

    #[test]
    #[ignore = "helper process spawned by already-exited shutdown test"]
    fn lifecycle_already_exited_fixture() {}

    #[test]
    #[ignore = "helper process spawned by forced-death recovery test"]
    fn lifecycle_single_rpc_response_fixture() {
        let mut request = String::new();
        std::io::stdin()
            .read_line(&mut request)
            .expect("RPC request");
        let request: Value = serde_json::from_str(request.trim()).expect("valid RPC request");
        let response = json!({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"recovered": true},
        });
        let mut stdout = std::io::stdout().lock();
        writeln!(stdout, "{}", response).expect("RPC response");
        stdout.flush().expect("flush RPC response");
    }

    #[test]
    fn proxy_env_var_list_contains_all_expected_names() {
        let expected = [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ];
        for name in expected {
            assert!(
                PROXY_ENV_VARS.contains(&name),
                "expected PROXY_ENV_VARS to contain {name}"
            );
        }
        assert_eq!(PROXY_ENV_VARS.len(), expected.len());
    }

    #[test]
    fn strip_proxy_env_vars_removes_inherited_proxy_vars_from_command() {
        // `Command::env_remove` records the removal on the command's own env
        // map regardless of whether the var is actually set in this process,
        // so we can assert on `get_envs()` without touching the real process
        // environment (which would make the test order-dependent under
        // parallel test execution).
        let mut command = Command::new("does-not-matter");
        command.env("SOME_OTHER_VAR", "kept");
        strip_proxy_env_vars(&mut command);

        // Windows env vars are case-insensitive, and `Command`'s internal env
        // map case-folds keys to match: setting HTTP_PROXY and http_proxy is
        // the same underlying variable, so lookups here must be case-insensitive
        // too or this test would fail to find entries under a differently-cased
        // key than the one `env_remove` happened to normalize to.
        let mut removed_lower = std::collections::HashSet::new();
        let mut kept_value = None;
        for (key, value) in command.get_envs() {
            let key_lower = key.to_string_lossy().to_lowercase();
            if value.is_none() {
                removed_lower.insert(key_lower);
            } else if key_lower == "some_other_var" {
                kept_value = value;
            }
        }

        for name in PROXY_ENV_VARS {
            assert!(
                removed_lower.contains(&name.to_lowercase()),
                "expected {name} to be tracked as removed"
            );
        }
        assert_eq!(kept_value, Some(std::ffi::OsStr::new("kept")));
    }
}
