// Package kit is the shared command kit of the zeo-cli/v1 contract
// (zeoarchitect SOW-05 rev d, ZEOCORE-SOW-34).
//
// An app registers its own commands and hooks and gets the kit verbs with one
// grammar, one exit-code table, one RESULT line and versioned JSON documents.
// This package uses the standard library only: an app that links it takes no
// third-party module.
//
// Slice 1 supplies the grammar, the exit table, the RESULT line and the
// help, version, doctor and status verbs. update, rollback, plan and apply
// are later slices.
package kit
