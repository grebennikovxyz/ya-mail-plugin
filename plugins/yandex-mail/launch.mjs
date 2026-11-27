// OpenClaw resolves this script against the installed bundle before spawning Node.
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { accessSync, chmodSync, closeSync, constants, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

const backend = join(dirname(fileURLToPath(import.meta.url)), "backend");
const version = /^version = "(\d+\.\d+\.\d+)"$/m.exec(readFileSync(join(backend, "pyproject.toml"), "utf8"))[1];
const win = process.platform === "win32";
const root = join(win ? process.env.LOCALAPPDATA || join(homedir(), "AppData", "Local") :
  process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "YaMailPlugin", "runtime");
const bin = join(root, version, "bin");
const app = join(bin, win ? "ya-mail.exe" : "ya-mail");
const uvVersion = "0.13.5";
const hashes = {
  "aarch64-apple-darwin":"743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73",
  "aarch64-pc-windows-msvc":"09f47edf90726084a70f9426f6e3ca4f46e9bf7cbf23b1716ec64d60b055fae3",
  "aarch64-unknown-linux-gnu":"4bb69cf95ed13f23713bb7bc09f241896ea0c75b7c557e615a20604b82f9c403",
  "aarch64-unknown-linux-musl":"7906c79adb21c81abd559841ba76bb1d9af9d791acac7a041b2972e03ae16538",
  "x86_64-apple-darwin":"b0f52b0e3945b4107d89024ea38e625683011fbfc05a0d98f9fa2d6dcee28c66",
  "x86_64-pc-windows-msvc":"a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a",
  "x86_64-unknown-linux-gnu":"f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f",
  "x86_64-unknown-linux-musl":"57ef9d9ccb01fb1bd998df995b0dd221b52f7d7841fbea93f2d4463bf61fb7a9"
};

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {windowsHide: true, stdio: ["ignore", 2, 2], ...options});
  if (result.error) throw result.error;
  if (result.status !== 0) throw Error(`Setup failed: ${command} (exit ${result.status})`);
  return result.stdout;
}

async function findUv() {
  const name = win ? "uv.exe" : "uv";
  const cache = join(root, "uv-" + uvVersion);
  const saved = join(cache, name);
  for (const dir of [cache, join(homedir(), ".local", "bin"), ...(process.env.PATH || "").split(delimiter)]) {
    if (!isAbsolute(dir)) continue;
    const candidate = join(dir, name);
    try { accessSync(candidate, constants.X_OK); return candidate; } catch {}
  }
  const arch = {x64: "x86_64", arm64: "aarch64"}[process.arch];
  const platform = {darwin: "apple-darwin", win32: "pc-windows-msvc", linux:
    process.report.getReport().header.glibcVersionRuntime ? "unknown-linux-gnu" : "unknown-linux-musl"}[process.platform];
  const target = arch + "-" + platform;
  if (!hashes[target]) throw Error("Install uv for this platform and retry Ya-Ma setup.");
  mkdirSync(cache, {recursive: true, mode: 0o700});
  const staging = mkdtempSync(join(cache, "install-"));
  try {
    const asset = "uv-" + target + (win ? ".zip" : ".tar.gz");
    const response = await fetch("https://github.com/astral-sh/uv/releases/download/" + uvVersion + "/" + asset, {signal: AbortSignal.timeout(120000)});
    if (!response.ok) throw Error("uv download failed: HTTP " + response.status);
    const bytes = Buffer.from(await response.arrayBuffer());
    if (createHash("sha256").update(bytes).digest("hex") !== hashes[target]) throw Error("uv checksum mismatch");
    const archive = join(staging, asset);
    writeFileSync(archive, bytes);
    const tar = win ? join(process.env.SystemRoot, "System32", "tar.exe") : "/usr/bin/tar";
    const entry = win ? "uv.exe" : "uv-" + target + "/uv";
    const file = join(staging, name);
    const fd = openSync(file, "wx", 0o700);
    try { run(tar, ["-xOf", archive, entry], {stdio: ["ignore", fd, 2]}); } finally { closeSync(fd); }
    chmodSync(file, 0o700);
    if (!existsSync(saved)) {
      try { renameSync(file, saved); } catch (e) { if (!existsSync(saved)) throw e; }
    }
  } finally { rmSync(staging, {recursive: true, force: true}); }
  return saved;
}

const [action, ...extra] = process.argv.slice(2);
if (!["serve", "prepare"].includes(action) || extra.length) throw Error("Expected serve or prepare");
if (!existsSync(app)) {
  const uv = await findUv();
  run(uv, ["--no-config", "tool", "install", "--python", "3.13", backend], {env: {
    ...process.env, UV_TOOL_DIR: join(root, version, "tools"), UV_TOOL_BIN_DIR: bin
  }});
}
if (action === "prepare") {
  process.stdout.write(app + "\n");
} else if (!win && typeof process.execve === "function") {
  process.execve(app, [app, "serve"], process.env);
} else {
  const child = spawn(app, ["serve"], {stdio: "inherit", windowsHide: true});
  for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, () => child.kill(signal));
  child.on("error", e => { console.error(e.message); process.exitCode = 1; });
  child.on("exit", code => { process.exitCode = code ?? 1; });
}
