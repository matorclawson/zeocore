// Package kit is the shared command kit of the zeo-cli/v1 contract
// (zeoarchitect SOW-05 rev d, ZEOCORE-SOW-34).
//
// An app registers its own commands and hooks and gets the kit verbs with one
// grammar, one exit-code table, one RESULT line and versioned JSON documents.
// This package uses the standard library only: an app that links it takes no
// third-party module.
//
// Slices 1 and 2 supply the grammar, the exit table, the RESULT line, the
// help, version, doctor and status verbs, and plan and apply over an app's
// Subjects (the plan document, its plan_id, the stale check, the backup and
// the undo). update, rollback and recover are later slices.
package kit
