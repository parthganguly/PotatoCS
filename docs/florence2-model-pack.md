# Florence 2 Model Pack

Florence 2 Basic local vision is optional in v0.2.0. The default installer and
normal app startup do not download Florence files, import Torch, or import
Transformers.

To stage the optional runtime and local model pack during a build/prep pass:

```powershell
$env:ODYSSEUS_INCLUDE_FLORENCE = "1"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-python-runtime.ps1
```

To prepare only the model pack:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-florence2-model.ps1
```

To prepare only the optional Florence runtime in the exact sidecar Python:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare-florence2-runtime.ps1
```

To verify an existing pack:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-florence2-model.ps1
```

Development pack location:

```text
models\florence2-base-ft
```

Installed-app pack locations:

```text
%APPDATA%\dev.odysseus.desktop\profiles\default\models\florence2-base-ft
%APPDATA%\dev.odysseus.desktop\models\florence2-base-ft
<Tauri resource dir>\models\florence2-base-ft
```

`ODYSSEUS_FLORENCE_MODEL_DIR` can override these locations with an explicit
pack directory. In `npm run tauri dev`, Rust also passes the real repo root as
`ODYSSEUS_DEV_REPO_ROOT`, so the sidecar can use `models\florence2-base-ft`
even though the Python backend is copied under `src-tauri\target\debug\python`.
Release builds do not guess a repo root from the copied backend path.

The pack is pinned to `microsoft/Florence-2-base-ft` revision
`f6c1a25888ffc1d945ee8a1a77ac833c7303d46e`, records file SHA-256 hashes in
`manifest.json`, and production loading uses local files only with
`trust_remote_code=false`.

`scripts\prepare-florence2-model.ps1` writes `manifest.json` as BOM-free
UTF-8. The verifier and runtime reader accept both BOM-free and BOM-prefixed
UTF-8 manifests, and they retain read-only compatibility with the legacy
`odysseus-florence2-manifest.json` name.

If the pack or optional runtime is missing, Diagnostics reports Florence as not
ready and Automatic routing falls back to the available native Ollama vision
path or OCR-only evidence.
