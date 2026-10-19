//go:build !linux && !darwin

package kit

import "syscall"

// ctimeOf is not read on this platform: the identity falls back to the mtime.
func ctimeOf(*syscall.Stat_t) int64 { return 0 }
