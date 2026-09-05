# Rust local model daemon operations

`embedding` and `ornith` are foreground Rust workers with no resident Python
processes. They can run under either the standalone user LaunchAgents installed
by this repository or the resident `context-stilld` supervisor. Do not enable
both ownership modes at the same time.

## Build and install assets

```bash
./scripts/install_rust_daemons.sh
./scripts/install_mistralrs.sh
./scripts/install_rust_model_assets.sh all
```

Configure ContextStill with the stable paths
`~/Library/Application Support/local-llm/bin/ornith` and
`~/Library/Application Support/local-llm/bin/embedding`. Compatibility symlinks
for the former `ornithd` / `embeddingd` paths are installed as well.

Artifacts are installed below
`~/Library/Application Support/local-llm/models/`. The committed manifests in
`models/manifests/` pin the source revision, byte length, and SHA-256.

The resident embedding artifact is the official qint8 ONNX export. Although its
upstream filename mentions AVX512, ONNX Runtime executes the graph on Apple
arm64 without that instruction set. The deployment gate measured minimum cosine
similarity 0.999286 against the previous Python/FP32 service, 601 MB idle
physical footprint, and no unsupported operator. The FP32 manifest remains as a
quality-first rollback option.

The mistral.rs binary is built from commit
`d184053f2441f897cf81429b98b0d868f4d96ff3` with only the Metal backend.
It is installed as `ornith-engine`; `mistralrs` remains only as a compatibility
symlink. `ornith` supervises that Rust process so it can change the context
profile without starting a Python interpreter.

## Activity Monitor names

- `ornith`: context-profile supervisor
- `ornith-engine`: Metal inference engine
- `embedding`: ONNX embedding worker

These are real executable filenames, so Activity Monitor and `ps` show the
same names. Internal service IDs and existing environment-variable prefixes
retain the `ornithd` / `embeddingd` spelling for compatibility.

## Lifecycle

```bash
./scripts/manage_rust_model_daemons.sh start
./scripts/manage_rust_model_daemons.sh status
./scripts/manage_rust_model_daemons.sh restart
./scripts/manage_rust_model_daemons.sh stop

# ContextStill-integrated lifecycle, when that supervisor is enabled:
context-stilld local-model status --json
context-stilld local-model start all
context-stilld local-model stop all
context-stilld local-model restart ornith --profile standard
context-stilld local-model profile set long
context-stilld local-model profile set standard
```

The standalone `stop` command unloads and disables both LaunchAgents. They stay
stopped across logout and login until an explicit standalone `start`. Before
switching to the ContextStill-integrated lifecycle, run the standalone `stop`
command first. Stop the ContextStill-managed workers before switching back to
the standalone lifecycle.

`standard` allocates a 32,768-token cache. `long` allocates 131,072 tokens and
uses an FP8 KV cache to keep the 128K profile viable on a 32 GB unified-memory
Mac. After `long` has processed at least one inference, `ornith` waits until
there are no in-flight requests or queued sequences. It then keeps a 30-second
idle grace period and automatically restarts Ornith in `standard`. A new
request during that grace period resets the timer. Merely selecting `long`
does not start the timer, so model loading and pre-task idle time cannot cause
an early fallback.
Set `ORNITHD_LONG_IDLE_SECONDS` (5–3600) only when the default grace period must
be changed.

Profile changes restart only Ornith; Embedding remains ready. Automatic
fallback also updates the ContextStill desired profile to `standard`, so a
later supervisor restart cannot restore 128K. `stop` writes the desired state
before terminating a worker, so login and supervisor restarts do not undo an
operator stop.

The Apple Silicon deployment check confirmed an effective 32,768-token BF16 KV
cache for `standard` and an effective 131,072-token F8E4M3 KV cache for `long`.
Both profiles completed a generation request. The persisted default is
`standard`; MTP remains `auto` with an effective value of `off`.

## Endpoints

- Ornith: `http://127.0.0.1:44448/v1`
- Embedding: `http://127.0.0.1:44512/embed`
- OpenAI Embeddings: `http://127.0.0.1:44512/v1/embeddings`
- Health: `GET /health` on both ports

## Rollback

Stop the Rust workers first. The old Python virtual environment and MLX model
assets are intentionally retained until the Rust canary has completed; they are
not part of the resident startup path.
