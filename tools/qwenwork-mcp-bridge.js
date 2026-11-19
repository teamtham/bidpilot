#!/usr/bin/env node
/**
 * A deliberately small, dependency-free MCP server for QwenWork.
 *
 * It gives QwenWork useful visibility into this project without granting a
 * general-purpose shell or unrestricted filesystem access.  The protocol is
 * JSON-RPC over stdin/stdout, as required by MCP's stdio transport.
 */

const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const execFileAsync = promisify(execFile);
const ROOT = path.resolve(process.env.QWENWORK_PROJECT_ROOT || process.cwd());
const MAX_OUTPUT = 30_000;
const IGNORED_DIRECTORIES = new Set([".git", "node_modules", ".next", "dist", "build", "coverage", "tmp"]);

function send(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function result(id, value) {
  send({ jsonrpc: "2.0", id, result: value });
}

function failure(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

function asText(text) {
  return { content: [{ type: "text", text }] };
}

function truncate(value, limit = MAX_OUTPUT) {
  return value.length > limit ? `${value.slice(0, limit)}\n… output truncated …` : value;
}

function resolveProjectPath(relativePath) {
  if (typeof relativePath !== "string" || relativePath.trim() === "") {
    throw new Error("A non-empty project-relative path is required.");
  }

  const resolved = path.resolve(ROOT, relativePath);
  if (resolved !== ROOT && !resolved.startsWith(`${ROOT}${path.sep}`)) {
    throw new Error("Path must stay inside the project root.");
  }

  const relative = path.relative(ROOT, resolved);
  if (relative.split(path.sep).some((part) => IGNORED_DIRECTORIES.has(part))) {
    throw new Error("That directory is intentionally unavailable through this bridge.");
  }
  return resolved;
}

async function command(command, args, options = {}) {
  try {
    const { stdout = "", stderr = "" } = await execFileAsync(command, args, {
      cwd: ROOT,
      timeout: options.timeout ?? 60_000,
      maxBuffer: MAX_OUTPUT * 2,
      env: { ...process.env, CI: "1", FORCE_COLOR: "0" },
    });
    return { exitCode: 0, stdout: truncate(stdout), stderr: truncate(stderr) };
  } catch (error) {
    return {
      exitCode: Number.isInteger(error.code) ? error.code : 1,
      stdout: truncate(error.stdout || ""),
      stderr: truncate(error.stderr || error.message || "Command failed."),
    };
  }
}

async function visibleFiles(directory = ROOT, prefix = "", depth = 0) {
  if (depth > 2) return [];
  const entries = await fsp.readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (IGNORED_DIRECTORIES.has(entry.name) || entry.name === ".DS_Store") continue;
    const relative = path.join(prefix, entry.name);
    if (entry.isDirectory()) {
      files.push(`${relative}/`);
      files.push(...(await visibleFiles(path.join(directory, entry.name), relative, depth + 1)));
    } else if (entry.isFile()) {
      files.push(relative);
    }
    if (files.length >= 200) break;
  }
  return files;
}

async function loadPackage() {
  try {
    return JSON.parse(await fsp.readFile(path.join(ROOT, "package.json"), "utf8"));
  } catch {
    return null;
  }
}

const tools = [
  {
    name: "project_summary",
    description: "Show the project root, visible files, package scripts, and Git working-tree state.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "read_project_file",
    description: "Read a UTF-8 text file inside the project. Paths outside the project and generated/dependency directories are blocked.",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string", description: "Project-relative path, for example src/app.ts" },
        max_lines: { type: "integer", minimum: 1, maximum: 1000, default: 300 },
      },
      required: ["path"],
    },
  },
  {
    name: "search_project",
    description: "Search project source files with a literal text query. This does not search dependencies, Git data, or temporary files.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Literal text to find" },
        max_results: { type: "integer", minimum: 1, maximum: 200, default: 60 },
      },
      required: ["query"],
    },
  },
  {
    name: "project_diff",
    description: "Show the current Git diff and status, if this project is a Git repository.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "run_project_check",
    description: "Run one declared package script: test, lint, build, or check. No arbitrary shell commands are accepted.",
    inputSchema: {
      type: "object",
      properties: {
        check: { type: "string", enum: ["test", "lint", "build", "check"], description: "The package.json script to run" },
      },
      required: ["check"],
    },
  },
  {
    name: "write_handoff",
    description: "Append a concise status note to QWENWORK_HANDOFF.md at the project root so another agent can pick up the work. This is the only write operation exposed by this bridge.",
    inputSchema: {
      type: "object",
      properties: {
        summary: { type: "string", description: "What changed or was learned" },
        next_steps: { type: "string", description: "What should happen next" },
      },
      required: ["summary", "next_steps"],
    },
  },
];

async function callTool(name, args = {}) {
  switch (name) {
    case "project_summary": {
      const [files, pkg, git] = await Promise.all([
        visibleFiles(),
        loadPackage(),
        command("git", ["status", "--short", "--branch"]),
      ]);
      const scripts = pkg?.scripts ? Object.keys(pkg.scripts).sort() : [];
      return asText([
        `Project root: ${ROOT}`,
        `Package scripts: ${scripts.length ? scripts.join(", ") : "none"}`,
        "",
        "Git status:", git.stdout || git.stderr || "Unavailable",
        "",
        "Visible files (depth ≤ 3):",
        files.length ? files.join("\n") : "(no project files yet)",
      ].join("\n"));
    }
    case "read_project_file": {
      const file = resolveProjectPath(args.path);
      const maxLines = Math.min(Math.max(Number(args.max_lines) || 300, 1), 1000);
      const content = await fsp.readFile(file, "utf8");
      const lines = content.split(/\r?\n/).slice(0, maxLines);
      const suffix = content.split(/\r?\n/).length > maxLines ? "\n… file truncated …" : "";
      return asText(`${args.path}\n\n${lines.join("\n")}${suffix}`);
    }
    case "search_project": {
      if (typeof args.query !== "string" || !args.query.trim()) throw new Error("query must be a non-empty string.");
      const maxResults = Math.min(Math.max(Number(args.max_results) || 60, 1), 200);
      const search = await command("rg", [
        "--line-number", "--no-heading", "--color", "never", "--fixed-strings",
        "--glob", "!.git", "--glob", "!node_modules", "--glob", "!tmp", "--glob", "!dist", "--glob", "!build",
        "--max-count", String(maxResults), args.query, ".",
      ]);
      if (search.exitCode > 1) return asText(`Search failed:\n${search.stderr}`);
      return asText(search.stdout || "No matches found.");
    }
    case "project_diff": {
      const [status, diff] = await Promise.all([
        command("git", ["status", "--short"]),
        command("git", ["diff", "--", "."]),
      ]);
      return asText(`Git status:\n${status.stdout || "clean"}\n\nGit diff:\n${diff.stdout || "(no unstaged diff)"}`);
    }
    case "run_project_check": {
      const allowed = new Set(["test", "lint", "build", "check"]);
      if (!allowed.has(args.check)) throw new Error("check must be one of: test, lint, build, check.");
      const pkg = await loadPackage();
      if (!pkg?.scripts?.[args.check]) throw new Error(`package.json does not declare a \"${args.check}\" script.`);
      const run = await command("npm", ["run", args.check], { timeout: 120_000 });
      return asText(`npm run ${args.check} (exit ${run.exitCode})\n\n${run.stdout}${run.stderr ? `\n${run.stderr}` : ""}`);
    }
    case "write_handoff": {
      if (typeof args.summary !== "string" || typeof args.next_steps !== "string") {
        throw new Error("summary and next_steps must both be strings.");
      }
      const timestamp = new Date().toISOString();
      const note = `\n## QwenWork update — ${timestamp}\n\n**Summary**\n${args.summary.trim()}\n\n**Next steps**\n${args.next_steps.trim()}\n`;
      await fsp.appendFile(path.join(ROOT, "QWENWORK_HANDOFF.md"), note, "utf8");
      return asText("Saved the hand-off note to QWENWORK_HANDOFF.md.");
    }
    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

async function handle(request) {
  if (!request || request.jsonrpc !== "2.0" || typeof request.method !== "string") return;
  const { id, method, params = {} } = request;
  if (method === "notifications/initialized") return;
  if (method === "initialize") {
    return result(id, {
      protocolVersion: "2025-03-26",
      capabilities: { tools: {} },
      serverInfo: { name: "qwenwork-project-bridge", version: "1.0.0" },
    });
  }
  if (method === "ping") return result(id, {});
  if (method === "tools/list") return result(id, { tools });
  if (method === "tools/call") {
    try {
      return result(id, await callTool(params.name, params.arguments));
    } catch (error) {
      return result(id, { content: [{ type: "text", text: `Tool error: ${error.message}` }], isError: true });
    }
  }
  if (id !== undefined) return failure(id, -32601, `Method not found: ${method}`);
}

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  const lines = buffer.split(/\r?\n/);
  buffer = lines.pop();
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      void handle(JSON.parse(line));
    } catch (error) {
      send({ jsonrpc: "2.0", error: { code: -32700, message: `Parse error: ${error.message}` } });
    }
  }
});

// Do not call process.exit() when stdin closes. Tool requests are asynchronous,
// and Node will exit naturally after their filesystem/child-process work settles.
