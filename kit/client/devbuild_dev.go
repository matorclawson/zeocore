//go:build zeodev

package client

// devBuild is true only in a binary built with the zeodev tag. Such a binary
// says so in version and doctor, and the install verb refuses to install it.
const devBuild = true
