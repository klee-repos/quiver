// Plain-JS (.mjs) runner for the Python data helper so `node decide.mjs` can
// import it without a build step. Spawns quill_data.py, returns parsed JSON.
// Read-only market-data fetch; fails safe to UNAVAILABLE on any error.
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function findRepoRoot() {
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
  return path.resolve(__dirname, "..", "..", "..");
}

const REPO = findRepoRoot();
const VENV_PY = process.env.QUIVER_PYTHON || path.join(REPO, ".venv", "bin", "python");

function findHelper() {
  const env = process.env.QUIVER_DATA_HELPER;
  if (env && fs.existsSync(env)) return env;
  let dir = __dirname;
  for (let i = 0; i < 8; i++) {
    const cand = path.join(dir, "quill_data.py");
    if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return cand;
    dir = path.resolve(dir, "..");
  }
  return path.join(REPO, "quiver_eve", "run", "quill_data.py");
}
const HELPER = findHelper();

export async function runPythonDataTool(kind, args) {
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
