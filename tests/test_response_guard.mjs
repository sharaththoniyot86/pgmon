// Tests app.js's non-JSON response guard (jsonAware/describeNonJson).
//
// Run with:  node tests/test_response_guard.mjs
//
// The guard is what stands between a user and "Failed: Unexpected token
// '<', "<!DOCTYPE "... is not valid JSON" -- a message that names neither
// the status code nor who answered. Each case below is a response shape
// that really does turn up in front of a self-hosted pgmon.
import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "app", "static", "js", "app.js"), "utf8");
// Lift the two functions out of the IIFE rather than copying them, so this
// test fails if they are edited into disagreement with it.
const start = src.indexOf("function describeNonJson");
const end = src.indexOf("const api = (path)");
if (start < 0 || end < 0 || end < start) {
  console.error("FAIL: could not locate describeNonJson/jsonAware in app.js");
  process.exit(1);
}
const { jsonAware } = new Function(src.slice(start, end) + "\nreturn {describeNonJson, jsonAware};")();

const fails = [];
function check(name, cond, extra = "") {
  console.log(`  ${cond ? "PASS" : "FAIL"}: ${name}${cond ? "" : " -- " + extra}`);
  if (!cond) fails.push(name);
}

function resp(body, { status = 200, statusText = "OK", url = "https://pgmon.local/api/queries/tuning-advice", redirected = false } = {}) {
  return Promise.resolve({
    status, statusText, url, redirected,
    ok: status >= 200 && status < 300,
    text: async () => body,
  });
}

async function parsed(r) {
  return await (await jsonAware(r)).json();
}
async function message(r) {
  try {
    await parsed(r);
    return null;
  } catch (e) {
    return e.message;
  }
}

console.log("\nJSON BODIES STILL PARSE UNTOUCHED");
check("success body", JSON.stringify(await parsed(resp('{"advice":[]}'))) === '{"advice":[]}');
check(
  "400 detail body",
  (await parsed(resp('{"detail":"Only a single statement is allowed."}', { status: 400 }))).detail ===
    "Only a single statement is allowed."
);
const twice = await jsonAware(resp('{"detail":"d"}', { status: 400 }));
check("json() twice (call sites do this)", (await twice.json()).detail === "d" && (await twice.json()).detail === "d");

console.log("\nNON-JSON BODIES GET AN EXPLANATION, NOT A PARSE ERROR");
const cases = [
  ["nginx welcome page, 200", resp("<!DOCTYPE html>\n<html><head><title>Welcome to nginx!</title>"), /web page instead of data \(HTTP 200/],
  ["pgmon login page", resp('<!doctype html>\n<html lang="en"><title>pgmon</title>'), /web page instead of data/],
  ["502 from the proxy", resp("<html>\r\n<head><title>502 Bad Gateway</title></head>", { status: 502, statusText: "Bad Gateway" }), /HTTP 502 \(Bad Gateway\)/],
  ["504 gateway timeout", resp("<html><head><title>504 Gateway Time-out</title>", { status: 504, statusText: "Gateway Time-out" }), /HTTP 504/],
  ["redirect landed on login", resp("<!doctype html><html>login</html>", { redirected: true, url: "https://pgmon.local/login?error=1" }), /session has expired/],
  ["plain-text 500", resp("Internal Server Error", { status: 500, statusText: "Internal Server Error" }), /internal error \(HTTP 500\)/],
  ["empty body", resp("", { status: 204 }), /empty body/],
  ["XML from a filtering proxy", resp('<?xml version="1.0"?><error/>', { status: 403 }), /web page instead of data \(HTTP 403/],
  ["truncated JSON", resp('{"advice": [{"tit', { status: 200 }), /Unreadable reply/],
];
for (const [name, r, expected] of cases) {
  const msg = await message(r);
  check(name, msg !== null && expected.test(msg), msg === null ? "parsed without error" : msg);
  check(`  ${name}: no raw parse-error text`, msg !== null && !/Unexpected token/.test(msg), msg || "");
}

console.log("\n" + (fails.length ? `FAILURES: ${fails.join(", ")}` : "ALL RESPONSE-GUARD TESTS PASSED"));
process.exit(fails.length ? 1 : 0);
