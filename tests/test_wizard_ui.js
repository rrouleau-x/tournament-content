// Node test for the wizard completion logic (admin_ui/app.js).
// Extracts and exercises the pure `wizardCompletion` decision the same
// way the browser would — this catches the mid-sequence-failure bug the
// reviewer found (a failed module PUT must not report its fields as
// complete). Run: node tests/test_wizard_ui.js
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const APP_JS = fs.readFileSync(
  path.join(__dirname, "..", "admin_ui", "app.js"), "utf8");

// Pull out the pure function only — the rest of app.js needs a DOM.
const match = APP_JS.match(/function wizardCompletion[\s\S]*?\n}/);
assert(match, "wizardCompletion not found in app.js");
// "use strict" eval doesn't leak declarations, so evaluate the
// declaration as a named function EXPRESSION and assign it.
const wizardCompletion = eval("(" + match[0] + ")");
assert.strictEqual(typeof wizardCompletion, "function", "eval failed");

// The server's 8-item checklist for a fresh scaffold
const CHECKLIST = [
  { module: "tournament.json", field: "tournament.name" },
  { module: "tournament.json", field: "tournament.dates.start" },
  { module: "tournament.json", field: "tournament.dates.end" },
  { module: "team.json", field: "team.name" },
  { module: "venue.json", field: "venue.name" },
  { module: "venue.json", field: "venue.address" },
  { module: "contacts.json", field: "contacts.manager" },
  { module: "contacts.json", field: "contacts.coach" },
];

// Case 1: ALL module writes confirmed → nothing remaining
{
  const done = new Set(CHECKLIST.map(c => c.field));
  const r = wizardCompletion(CHECKLIST, done, []);
  assert.strictEqual(r.failures.length, 0);
  assert.deepStrictEqual(r.remaining, []);
  console.log("PASS: all writes confirmed → nothing remaining");
}

// Case 2: THE BUG — team.json PUT failed mid-sequence (409). Only
// tournament.name was confirmed; team/venue/contacts writes never ran.
// The old code subtracted ALL eight wizard fields; the correct result
// keeps team.name + venue + contacts as remaining.
{
  const done = new Set(["tournament.name"]); // only tournament.json succeeded
  const failures = [{ file: "team.json", error: "conflict" }];
  const r = wizardCompletion(CHECKLIST, done, failures);
  assert.strictEqual(r.failures.length, 1);
  const remainingFields = r.remaining.map(c => c.field);
  assert(remainingFields.includes("team.name"),
         "team.name must remain incomplete after team.json PUT failed");
  assert(remainingFields.includes("venue.name"),
         "venue.name must remain incomplete (write never ran)");
  assert(remainingFields.includes("contacts.manager"),
         "contacts.manager must remain incomplete (write never ran)");
  assert(!remainingFields.includes("tournament.name"),
         "tournament.name was confirmed written — must be complete");
  console.log("PASS: team.json failure → team/venue/contacts stay incomplete");
}

// Case 3: dates left empty in the wizard → dates fields stay incomplete
// even though tournament.json was written (the module only carries name)
{
  const done = new Set(["tournament.name", "team.name",
                        "venue.name", "venue.address",
                        "contacts.manager", "contacts.coach"]);
  const r = wizardCompletion(CHECKLIST, done, []);
  const remainingFields = r.remaining.map(c => c.field);
  assert(remainingFields.includes("tournament.dates.start"), "empty start date stays incomplete");
  assert(remainingFields.includes("tournament.dates.end"), "empty end date stays incomplete");
  assert.strictEqual(r.failures.length, 0);
  console.log("PASS: un-entered dates stay incomplete (module didn't carry them)");
}

// Case 4: partial failure on the 4th PUT (contacts) — earlier writes count
{
  const done = new Set(["tournament.name", "team.name", "venue.name", "venue.address"]);
  const failures = [{ file: "contacts.json", error: "409 conflict" }];
  const r = wizardCompletion(CHECKLIST, done, failures);
  const remainingFields = r.remaining.map(c => c.field);
  assert(remainingFields.includes("contacts.manager"));
  assert(remainingFields.includes("contacts.coach"));
  assert(!remainingFields.includes("venue.name"));
  console.log("PASS: 4th-PUT failure → contacts remain, earlier writes honored");
}

console.log("\nAll wizard completion cases pass.");
