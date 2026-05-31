#!/usr/bin/env node
/**
 * Postinstall script: ensures the Python teamcache package is installed.
 * Tries pip, pip3, then uv in order.
 */
const { execSync, spawnSync } = require("child_process");
const { existsSync } = require("fs");
const path = require("path");

function tryInstall(pip) {
  const result = spawnSync(pip, ["install", "--quiet", "teamcache"], {
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  return result.status === 0;
}

function isInstalled() {
  const result = spawnSync("teamcache", ["--version"], {
    stdio: "pipe",
    shell: process.platform === "win32",
  });
  return result.status === 0;
}

if (isInstalled()) {
  process.exit(0);
}

const candidates = ["pip", "pip3", "uv pip"];
for (const pip of candidates) {
  if (tryInstall(pip)) {
    console.log(`teamcache installed via ${pip}`);
    process.exit(0);
  }
}

console.error(
  "Could not install teamcache automatically.\n" +
    "Please run: pip install teamcache"
);
process.exit(1);
