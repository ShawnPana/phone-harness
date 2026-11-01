#!/usr/bin/env node
// Writes skills/phone-harness/SKILL.md from the pinned phone-harness release, so the skill the
// agent reads always matches the helpers it runs. Takes the release's own skill text and puts
// OpenClaw frontmatter and scripts/skill-preface.md in front of it. Needs uv and the network.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PHONE_HARNESS_VERSION as version } from "../dist/pin.js";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

const skill = execFileSync(
  "uvx",
  ["--quiet", "--python", "3.12", "--from", `phone-harness==${version}`, "phone-harness", "skill"],
  { encoding: "utf8", env: { ...process.env, PHONE_HARNESS_TELEMETRY: "0" } },
);

const parts = skill.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
if (!parts) {
  throw new Error(`phone-harness ${version} printed a skill with no frontmatter`);
}
const [, frontmatter, body] = parts;
const preface = readFileSync(path.join(root, "scripts", "skill-preface.md"), "utf8");

const out = [
  "---",
  frontmatter,
  "homepage: https://phone-harness.com",
  "metadata:",
  "  openclaw:",
  "    requires:",
  '      bins: ["uvx"]',
  "---",
  "",
  preface.replaceAll("VERSION", version) + body.trimStart(),
].join("\n");

const target = path.join(root, "skills", "phone-harness", "SKILL.md");
mkdirSync(path.dirname(target), { recursive: true });
writeFileSync(target, out);
console.log(`wrote ${path.relative(root, target)} from phone-harness ${version}`);
