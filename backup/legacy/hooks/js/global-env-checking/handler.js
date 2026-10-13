console.log("[global-env-checking] MODULE LOADED");
process.stderr.write("[global-env-checking] MODULE LOADED (stderr)\n");

const { execFile } = require("node:child_process");
const { promisify } = require("node:util");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");

const execFileAsync = promisify(execFile);

const PYTHON_VERSION = process.env.SKILL_PYTHON_VERSION || "3.13";
const COMMAND_TIMEOUT = Number(
  process.env.ENV_CHECK_TIMEOUT_MS || 120_000
);

const QUICK_CHECK_TIMEOUT = 10_000;

function log(message, ...args) {
  console.log(
    `[env-unified] ${new Date().toISOString()} ${message}`,
    ...args
  );
}

function warn(message, ...args) {
  console.warn(
    `[env-unified] ${new Date().toISOString()} ${message}`,
    ...args
  );
}
const LOCK_PATH =
  process.env.ENV_CHECK_LOCK || path.join(os.tmpdir(), "global-env-check.lock");

// ============================================================================
// Configuration
// ============================================================================
const SKILL_ENVS = [
  {
    name: "asr",
    venvPath: "/home/node/venv_asr",
    pipPackages: ["av", "python-dotenv", "openai", "numpy"],
    nodeModules: [],
  },
  {
    name: "iti",
    venvPath: "/home/node/venv_iti",
    pipPackages: [
      "aiohttp",
      "aiofiles",
      "python-dotenv",
      "openai",
      "lark-oapi",
    ],
    nodeModules: [],
  },
  {
    name: "pdf",
    venvPath: "/home/node/venv_pdf",
    pipPackages: [
      "pypdf",
      "pdfplumber",
      "reportlab",
      "pytesseract",
      "pdf2image",
      "pandas",
      "openpyxl",
    ],
    nodeModules: ["pdf-lib", "pdf-parse"],
  },
  {
    name: "ppt",
    venvPath: "/home/node/venv_ppt",
    pipPackages: [
      "markitdown[pptx]",
      "Pillow",
      "defusedxml",
      "lxml",
    ],
    nodeModules: [
      "pptxgenjs",
      "sharp",
      "react-icons",
      "react",
      "react-dom",
    ],
  },
  {
    name: "video",
    venvPath: "/home/node/venv_video",
    pipPackages: [
      "aiohttp",
      "aiofiles",
      "python-dotenv",
      "openai",
      "lark-oapi",
      "volcengine-python-sdk[ark]",
    ],
    nodeModules: [],
  },
  {
    name: "tti",
    venvPath: "/home/node/venv_tti",
    pipPackages: [
      "aiohttp",
      "aiofiles",
      "python-dotenv",
      "openai",
      "lark-oapi",
    ],
    nodeModules: [],
  },
  {
    name: "xlsx",
    venvPath: "/home/node/venv_xlsx",
    pipPackages: ["openpyxl", "pandas", "markitdown"],
    nodeModules: [],
  },
];

const WORKSPACE_NODE_MODULES = ["docx"];
const SYSTEM_PACKAGES = ["zip", "unzip"];

// ============================================================================
// Process helpers
// ============================================================================
async function run(command, args = [], options = {}) {
  const timeout = options.timeout ?? COMMAND_TIMEOUT;
  const startedAt = Date.now();

  log(`run: ${command} ${args.join(" ")}`);

  try {
    const result = await execFileAsync(command, args, {
      timeout,
      maxBuffer: 10 * 1024 * 1024,
      windowsHide: true,
      ...options,
      timeout,
    });

    log(
      `done: ${command}, ${Date.now() - startedAt}ms`
    );

    return result;
  } catch (err) {
    const duration = Date.now() - startedAt;
    const reason = err.killed
      ? `timeout after ${timeout}ms`
      : err.message;

    warn(`failed: ${command}, ${duration}ms, ${reason}`);

    throw err;
  }
}


async function commandExists(command) {
  try {
    await run("sh", ["-lc", `command -v "$1"` , "--", command], {
      timeout: QUICK_CHECK_TIMEOUT,
    });

    return true;
  } catch {
    return false;
  }
}


async function isExecutable(filePath) {
  try {
    await fs.access(filePath, require("node:fs").constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// ============================================================================
// uv resolution and installation
// ============================================================================
async function resolveUv() {
  if (process.env.UV_BINARY && (await isExecutable(process.env.UV_BINARY))) {
    return process.env.UV_BINARY;
  }

  if (await commandExists("uv")) {
    try {
      const { stdout } = await run("sh", ["-lc", "command -v uv"], {
        timeout: 10_000,
      });
      const resolved = stdout.trim();
      if (resolved) return resolved;
    } catch {
      // Continue with known locations.
    }
  }

  const home = process.env.HOME || "/home/node";
  const candidates = [
    path.join(home, ".local", "bin", "uv"),
    path.join(home, ".cargo", "bin", "uv"),
    "/home/node/.local/bin/uv",
    "/home/node/.cargo/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
  ];

  for (const candidate of candidates) {
    if (await isExecutable(candidate)) {
      return candidate;
    }
  }

  return null;
}

async function ensureUv() {
  const existing = await resolveUv();
  if (existing) return existing;

  const home = process.env.HOME || "/home/node";
  const installDir = process.env.UV_INSTALL_DIR || path.join(home, ".local", "bin");
  const installerPath = path.join(os.tmpdir(), "uv-install.sh");

  try {
    await fs.mkdir(installDir, { recursive: true });

    console.log(`[env-unified] uv not found, installing into ${installDir}...`);

    await run(
      "curl",
      [
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "https://astral.sh/uv/install.sh",
        "--output",
        installerPath,
      ],
      { timeout: 60_000 }
    );

    await run("sh", [installerPath], {
      env: {
        ...process.env,
        UV_INSTALL_DIR: installDir,
        CARGO_HOME: process.env.CARGO_HOME || path.join(home, ".cargo"),
      },
    });

    const installed = await resolveUv();
    if (!installed) {
      throw new Error("uv installation completed but executable was not found");
    }

    return installed;
  } catch (err) {
    console.error("[env-unified] Failed to install uv:", err.message);
    return null;
  } finally {
    await fs.rm(installerPath, { force: true }).catch(() => {});
  }
}

// ============================================================================
// Locking
// ============================================================================
async function acquireLock() {
  try {
    await fs.mkdir(LOCK_PATH);
    await fs.writeFile(
      path.join(LOCK_PATH, "owner"),
      `${process.pid}\n${new Date().toISOString()}\n`,
      "utf8"
    );

    return async () => {
      await fs.rm(LOCK_PATH, { recursive: true, force: true }).catch(() => {});
    };
  } catch (err) {
    if (err.code !== "EEXIST") throw err;

    // Remove only locks that are clearly stale.
    try {
      const stat = await fs.stat(LOCK_PATH);
      const age = Date.now() - stat.mtimeMs;

      if (age > 30 * 60 * 1000) {
        console.warn("[env-unified] Removing stale environment lock");
        await fs.rm(LOCK_PATH, { recursive: true, force: true });
        return acquireLock();
      }
    } catch {
      // Another process may have removed the lock.
    }

    return null;
  }
}

// ============================================================================
// Python environment setup
// ============================================================================
async function ensurePythonVenv(env, uvPath) {
  const { name, venvPath, pipPackages } = env;
  const pythonBin = path.join(venvPath, "bin", "python");

  try {
    await fs.mkdir(path.dirname(venvPath), { recursive: true });

    let pythonReady = await isExecutable(pythonBin);

    if (!pythonReady) {
      console.log(`[env-unified] Creating ${name} venv...`);

      await run(
        uvPath,
        [
          "venv",
          "--python",
          PYTHON_VERSION,
          "--seed",
          "--allow-existing",
          venvPath,
        ],
        { timeout: COMMAND_TIMEOUT }
      );

      pythonReady = await isExecutable(pythonBin);
    }

    if (!pythonReady) {
      throw new Error(`Python executable not found: ${pythonBin}`);
    }

    if (pipPackages.length > 0) {
      // uv pip does not require pip to be installed inside the venv.
      await run(
        uvPath,
        [
          "pip",
          "install",
          "--python",
          pythonBin,
          "--upgrade",
          ...pipPackages,
        ],
        { timeout: COMMAND_TIMEOUT }
      );
    }

    return {
      ok: true,
      message: `✅ ${name} venv ready (packages ok)`,
    };
  } catch (err) {
    return {
      ok: false,
      message: `❌ ${name} venv setup failed: ${formatError(err)}`,
    };
  }
}

// ============================================================================
// Node module setup
// ============================================================================
async function ensureNodeModules(modules, workspaceDir) {
  if (!modules || modules.length === 0) {
    return "No Node modules required";
  }

  log(`node phase started: ${modules.join(", ")}`);
  log(`node workspace: ${workspaceDir}`);

  try {
    await fs.mkdir(workspaceDir, { recursive: true });
  } catch (err) {
    return `❌ Workspace unavailable: ${formatError(err)}`;
  }

  const missing = [];

  for (const moduleName of modules) {
    log(`checking Node module: ${moduleName}`);

    try {
      await run(
        process.execPath,
        ["-e", "require.resolve(process.argv[1])", moduleName],
        {
          cwd: workspaceDir,
          timeout: QUICK_CHECK_TIMEOUT,
        }
      );

      log(`Node module ready: ${moduleName}`);
    } catch {
      log(`Node module missing: ${moduleName}`);
      missing.push(moduleName);
    }
  }

  if (missing.length === 0) {
    log("node phase complete: all modules already installed");
    return "✅ All Node modules already installed";
  }

  log(`npm install started: ${missing.join(", ")}`);

  try {
    await run(
      "npm",
      [
        "install",
        "--no-audit",
        "--no-fund",
        "--prefer-offline",
        ...missing,
      ],
      {
        cwd: workspaceDir,
        timeout: 120_000,
        env: {
          ...process.env,
          npm_config_loglevel: "warn",
          npm_config_fetch_timeout: 30000,
          npm_config_fetch_retries: 1,
        },
      }
    );

    log(`npm install complete: ${missing.join(", ")}`);
    return `✅ Installed Node modules: ${missing.join(", ")}`;
  } catch (err) {
    warn(`npm install failed: ${formatError(err)}`);
    return `❌ Node modules install failed: ${formatError(err)}`;
  }
}


async function ensureSystemPackages(packages) {
  const missing = [];

  for (const pkg of packages) {
    if (!(await commandExists(pkg))) {
      missing.push(pkg);
    }
  }

  if (missing.length === 0) {
    return "✅ All system packages already installed";
  }

  // OpenClaw 容器默认以 node 用户运行；镜像未提供 sudo。
  // 非 root 时不尝试提权，避免 spawn sudo ENOENT。
  if (typeof process.getuid === "function" && process.getuid() !== 0) {
    return `⚠️ Missing system tools: ${missing.join(", ")}. `
      + "Run: docker compose exec -u 0 openclaw-gateway "
      + `apt-get install -y ${missing.join(" ")}`;
  }

  if (!(await commandExists("apt-get"))) {
    return `⚠️ No known package manager found, missing: ${missing.join(", ")}`;
  }

  try {
    await run("apt-get", ["update"], { timeout: 60_000 });
    await run("apt-get", ["install", "-y", ...missing], {
      timeout: 120_000,
    });
    return `✅ Installed system packages: ${missing.join(", ")}`;
  } catch (err) {
    return `❌ System package install failed: ${formatError(err)}`;
  }
}


function formatError(err) {
  const details = [err?.message, err?.stderr, err?.stdout]
    .filter(Boolean)
    .join(" | ")
    .replace(/\s+/g, " ")
    .trim();

  return details || String(err);
}

// ============================================================================
// Main handler
// ============================================================================
module.exports = async function handler(event = {}) {
  console.log("[global-env-checking] HANDLER CALLED");

  const isNewCommand =
    event.type === "command" && event.action === "new";

  const isBootstrap =
    event.type === "agent" && event.action === "bootstrap";

  if (!isNewCommand && !isBootstrap) {
    log(`ignored event: type=${event.type}, action=${event.action}`);
    return;
  }

  const isInteractive = isNewCommand;
  const workspace =
    event.context?.workspaceDir ||
    process.env.WORKSPACE_DIR ||
    process.cwd();

  log(
    `accepted event: type=${event.type}, action=${event.action}, workspace=${workspace}`
  );

  const statusMessages = [];
  event.messages = Array.isArray(event.messages) ? event.messages : [];

  log("acquiring environment lock");
  const releaseLock = await acquireLock();

  if (!releaseLock) {
    warn("environment setup is already running in another process");

    if (isInteractive) {
      event.messages.push(
        "🔧 Environment check:\nℹ️ Environment setup already running in another process."
      );
    }

    return;
  }

  log("environment lock acquired");

  try {
    log("phase 1: system tools");

    const systemResult = await ensureSystemPackages(SYSTEM_PACKAGES);
    log(`phase 1 complete: ${systemResult}`);

    if (isInteractive) {
      statusMessages.push(`🛠️ System tools: ${systemResult}`);
    }

    log("phase 2: workspace Node modules");

    const workspaceModulesResult = await ensureNodeModules(
      WORKSPACE_NODE_MODULES,
      workspace
    );

    log(`phase 2 complete: ${workspaceModulesResult}`);

    if (isInteractive) {
      statusMessages.push(`📦 Workspace Node: ${workspaceModulesResult}`);
    }

    log("phase 3: resolving uv");

    const uvPath = await ensureUv();

    if (!uvPath) {
      warn("phase 3 failed: uv unavailable");

      if (isInteractive) {
        statusMessages.push(
          "🐍 Python venvs: ❌ uv unavailable; Python environments could not be repaired."
        );
      }
    } else {
      log(`uv resolved: ${uvPath}`);
      log("phase 4: Python environments");

      const venvResults = await Promise.all(
        SKILL_ENVS.map((env) => ensurePythonVenv(env, uvPath))
      );

      for (const result of venvResults) {
        log(result.message);
      }

      log("phase 4 complete");

      if (isInteractive) {
        const venvStatus = venvResults
          .map((result, index) => {
            return `${SKILL_ENVS[index].name}: ${result.message}`;
          })
          .join("; ");

        statusMessages.push(`🐍 Python venvs: ${venvStatus}`);
      }
    }

    log("phase 5: skill Node modules");

    const nodeResults = await Promise.all(
      SKILL_ENVS.map(async (env) => {
        if (!env.nodeModules || env.nodeModules.length === 0) {
          return null;
        }

        return {
          name: env.name,
          result: await ensureNodeModules(env.nodeModules, workspace),
        };
      })
    );

    log("phase 5 complete");

    if (isInteractive) {
      const nodeStatus = nodeResults
        .filter(Boolean)
        .map((result) => `${result.name}: ${result.result}`)
        .join("; ");

      if (nodeStatus) {
        statusMessages.push(`📦 Skill Node: ${nodeStatus}`);
      }

      event.messages.push(
        `🔧 Environment check:\n${statusMessages.join("\n")}`
      );
    }

    log("environment check complete");
  } catch (err) {
    console.error("[env-unified] unexpected handler failure:", err);

    if (isInteractive) {
      event.messages.push(
        `🔧 Environment check:\n❌ Unexpected failure: ${formatError(err)}`
      );
    }
  } finally {
    await releaseLock();
    log("environment lock released");
  }
};

