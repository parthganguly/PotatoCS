# Third-Party Notices

Odysseus Desktop is a Windows-first desktop MVP derived from the upstream
Odysseus project:

- Repository: https://github.com/pewdiepie-archdaemon/odysseus
- License: MIT
- Upstream acknowledgments: https://github.com/pewdiepie-archdaemon/odysseus/blob/main/ACKNOWLEDGMENTS.md

The desktop MVP preserves the upstream MIT license notice in `LICENSE`.

## Bundled Runtime and Libraries

The Windows release bundle stages the Python embeddable distribution from
python.org into `python-runtime`. The runtime is used only for the local
JSON-RPC sidecar and does not expose an HTTP service.

The MVP also uses these major third-party components:

- Tauri and Rust crates, under their respective crate licenses.
- React and React DOM, MIT license.
- lucide-react icons, ISC license.
- NumPy, BSD-style license.
- pypdf, BSD-style license.
- ReportLab, BSD-style license.
- Pillow, HPND-style license.
- arboard Rust crate, MIT or Apache-2.0 license.
- image Rust crate, MIT or Apache-2.0 license.
- screenshots Rust crate, MIT license.

Node, Rust, and Python transitive dependency license details should be reviewed
from `package-lock.json`, `src-tauri/Cargo.lock`, and the installed Python
package metadata before public redistribution.

## Optional External OCR Tools

OCR engines are not bundled in the base app. Odysseus Desktop only detects and
executes locally installed tools when available:

- Tesseract OCR
- Poppler `pdftoppm`
- MuPDF `mutool`

If a distributor chooses to bundle any OCR tool in the future, that distributor
must include the corresponding license terms for that tool.

## Optional Florence 2 Basic Local Vision

Florence 2 Basic is not bundled in the default runtime. When explicitly staged
with `ODYSSEUS_INCLUDE_FLORENCE=1`, Odysseus Desktop prepares a local model pack
for:

- `microsoft/Florence-2-base-ft`
- License: MIT
- Pinned revision: `f6c1a25888ffc1d945ee8a1a77ac833c7303d46e`

The optional Florence runtime uses pinned Python packages listed in
`python/requirements-florence.txt`, including PyTorch, Transformers,
Safetensors, Tokenizers, and Hugging Face Hub. Normal app startup does not
download Florence model files and production loading uses local files only with
`trust_remote_code=false`.
