// TS runner for the Python data helper. Spawns quill_data.py, returns the
// parsed JSON {report, core_available}. Best-effort: a non-zero exit or bad
// JSON returns an UNAVAILABLE report so the brain marks it missing (fail-safe).
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// EVE snapshots authored modules into .eve/dev-runtime/snapshots/<id>/source/...
// at runtime, so __dirname is NOT the source tree. Resolve the real repo root
// from QUIVER_REPO (set by analyze.py / the box), else walk up from __dirname
// until we find pyproject.toml (the quiver root marker).
function findRepoRoot(): string {
  if (process.env.QUIVER_REPO && fs.existsSync(path.join(process.env.QUIVER_REPO, "pyproject.toml"))) {
    return process.env.QUIVER_REPO;
  }
  let dir = __dirname;
  for (let i = 0; i < 8; i++) {
    if (fs.existsSync(path.join(dir, "pyproject.toml")) && fs.existsSync(path.join(dir, "analyze.py"))) {
      return dir;
    }
    dir = path.resolve(dir, "..");
  }
  // Last-resort fallback (correct in the source tree, wrong in the snapshot,
  // but the helper degrades to UNAVAILABLE rather than crashing).
  return path.resolve(__dirname, "..", "..", "..");
}
import fs from "node:fs";
const REPO = findRepoRoot();
const VENV_PY = process.env.QUIVER_PYTHON || path.join(REPO, ".venv", "bin", "python");
// The .py helper path. EVE bundles .ts modules into node_modules/snapshots at runtime,
// so __dirname is NOT reliable. Prefer an explicit QUIVER_DATA_HELPER (set by analyze.py,
// which knows the real source path); else walk up from __dirname for quill_data.py;
// else use the source-tree relative path from REPO.
function findHelper(): string {
  const env = process.env.QUIVER_DATA_HELPER;
  if (env && fs.existsSync(env)) return env;
  // Walk up from __dirname (covers the snapshot case where the .py sits next to the .ts)
  let dir = __dirname;
  for (let i = 0; i < 8; i++) {
    const cand = path.join(dir, "quill_data.py");
    if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return cand;
    dir = path.resolve(dir, "..");
  }
  return path.join(REPO, "quiver_eve", "run", "quill_data.py");
}
const HELPER = findHelper();

export async function runPythonDataTool(kind: string, args: Record<string, string | undefined>): Promise<{ report: string; core_available: boolean }> {
  const ticker = args.ticker || "";
  const date = args.date ? ["--date", args.date] : [];
  return new Promise((resolve) => {
    const p = spawn(VENV_PY, [HELPER, kind, ticker, ...date], { cwd: REPO });
    let stdout = "";
    let stderr = "";
    p.stdout.on("data", (d) => { stdout += d.toString(); });
    p.stderr.on("data", (d) => { stderr += d.toString(); });
    p.on("close", (code) => {
      if (code !== 0) {
        resolve({ report: `UNAVAILABLE: quill_data exit ${code}: ${stderr.slice(0, 200)}`, core_available: false });
        return;
      }
      try {
        const line = stdout.trim().split("\n").pop() || "{}";
        resolve(JSON.parse(line));
      } catch {
        resolve({ report: "UNAVAILABLE: bad JSON from quill_data", core_available: false });
      }
    });
    p.on("error", () => resolve({ report: "UNAVAILABLE: spawn failed", core_available: false }));
  });
}
