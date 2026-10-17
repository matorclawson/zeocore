//go:build !zeodev

package client

// devBuild is false in a release build: a missing host file is a refusal.
const devBuild = false
